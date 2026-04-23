"""基于 LiteLLM 的 LLM 客户端封装。

所有 LLM 调用都走这里。上层调用栈是：

    AgentLoop
      │
      ▼
    LLMClient.chat(messages, tools=...)    ← 这个文件
      │
      ├── LLMCache（命中就直接返回）
      │
      ├── LiteLLM.completion()              ← 真正发 HTTP
      │
      └── 回写 LLMCache + 发 LLMTrace

套这一层的三个理由：

  1. provider 无关。上层永远调 LLMClient.chat，不直接碰 openai / anthropic
     SDK。换 provider 就改一行 model 字符串。
  2. 成本和确定性护栏集中在这一层。Agent 逻辑不用每处都记得限流、缓存、
     trace——一次做好兜住。
  3. tool-calling 响应结构稳定。LiteLLM 已经把各家 provider 的响应归一化到
     OpenAI 风格（message.tool_calls），这里再包一层 Pydantic，让上游 Agent
     拿到强类型对象。

预算做两层：GAMEGUARD_USD_BUDGET 是主控，超了抛 BudgetExceeded；
GAMEGUARD_TOKEN_BUDGET 是保底，防某些 provider 定价数据缺失时仍能限住。
LiteLLM 的 usage.prompt_tokens / completion_tokens 是各家都有的字段；
response.cost 是 LiteLLM 按定价表估的，对那些没定价表的 provider（比如
一些国产私有化模型）拿不到 cost，就退化成按 token 估。
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import litellm
from litellm import completion as litellm_completion
from pydantic import BaseModel, Field

from gameguard.llm.cache import LLMCache
from gameguard.llm.trace import LLMTrace

# --------------------------------------------------------------------------- #
# 请求 / 响应的 Pydantic 模型
# --------------------------------------------------------------------------- #

class ToolCall(BaseModel):
    """LLM 请求调用的某个 tool。"""

    id: str
    name: str
    arguments: dict[str, Any]

class LLMResponse(BaseModel):
    """归一化之后的 LLM 响应（一次 completion 的结果）。"""

    model: str
    content: str = ""
    tool_calls: list[ToolCall] = Field(default_factory=list)
    # 可观测性字段
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost_usd: float = 0.0
    cached: bool = False
    # 原始响应（debug 用，trace 里落完整版）
    raw: dict[str, Any] = Field(default_factory=dict)

    @property
    def finished(self) -> bool:
        """没有 tool_calls 就视为本轮结束。AgentLoop 的 while 判据。"""
        return not self.tool_calls

# --------------------------------------------------------------------------- #
# 预算异常
# --------------------------------------------------------------------------- #

class BudgetExceeded(RuntimeError):
    """美元或 token 预算被用完；立即中止 agent，避免继续花钱。"""

# --------------------------------------------------------------------------- #
# LLMClient
# --------------------------------------------------------------------------- #

@dataclass
class LLMClient:
    model: str
    cache: LLMCache
    trace: LLMTrace
    # 预算（None 则不限）
    usd_budget: float | None = None
    token_budget: int | None = None
    # 默认参数
    temperature: float = 0.0
    # 8192 是为 GLM-4.7 / Claude thinking 等推理型模型留的余量：
    # 它们会把一部分额度消耗在 reasoning_content 上，再输出 tool_calls。
    # 预算太紧会看到"空 content + 空 tool_calls"的静默失败。
    max_tokens: int = 8192
    # 名字：用于 trace 里区分不同 agent 共用同一 client 的情况
    default_agent: str = "unknown"
    debug: bool = False
    # 内部：累计花费 / token
    used_usd: float = 0.0
    used_tokens: int = 0
    # 附加 kwargs（例如智谱需要的 api_base override）
    extra_kwargs: dict[str, Any] = field(default_factory=dict)
    # 关闭推理型模型（GLM-4.7、GLM-5、Claude thinking、o1/o3）的"内置思考"。
    # 在 tool-calling 场景下打开此开关常常能消除"max_tokens 全部花在
    # reasoning_content 上、tool_calls 输出空"的静默失败模式。
    # 通过 LiteLLM 的 extra_body 透传给底层 provider（智谱 API 接受
    # ``{"thinking": {"type": "disabled"}}``）。
    disable_thinking: bool = False

    # ---- 类方法：从 env 组装一个默认 client -----------------------------------

    @classmethod
    def from_env(
        cls,
        *,
        trace_path: str | Path,
        session_id: str,
        model: str | None = None,
        default_agent: str = "unknown",
    ) -> "LLMClient":
        """读取 .env / 系统环境变量，组装一个生产级客户端。

        这是大多数调用方的入口：把读 env / 装配 Cache / Trace / Budget
        的 boilerplate 封一次，Agent 代码就能 ``client = LLMClient.from_env(...)``
        一行搞定。

        关于智谱（GLM）/ Z.AI：
            LiteLLM 1.83 原生支持 ``zai/`` 前缀（Z.AI 是智谱的海外品牌），
            模型直接写 ``zai/glm-4.6``、``zai/glm-4.5`` 等；默认 api_base
            是 ``https://api.z.ai/api/paas/v4``，API key 读 ``ZAI_API_KEY``。
            如果用户想走智谱国内站 (``open.bigmodel.cn``)，在 .env 里额外
            设置 ``ZHIPU_API_BASE=https://open.bigmodel.cn/api/paas/v4/``
            即可覆盖（见下方 ``_resolve_provider``）。
        """
        model = model or os.environ.get("GAMEGUARD_MODEL", "zhipu/glm-4.6")
        cache_dir = Path(os.environ.get("GAMEGUARD_CACHE_DIR", ".cache/llm"))
        strict = os.environ.get("GAMEGUARD_DETERMINISTIC", "0") == "1"
        cache = LLMCache(root=cache_dir, strict=strict)
        trace = LLMTrace(path=Path(trace_path), session_id=session_id)

        usd_budget_str = os.environ.get("GAMEGUARD_USD_BUDGET")
        token_budget_str = os.environ.get("GAMEGUARD_TOKEN_BUDGET")

        resolved_model, extra = _resolve_provider(model)

        return cls(
            model=resolved_model,
            cache=cache,
            trace=trace,
            usd_budget=float(usd_budget_str) if usd_budget_str else None,
            token_budget=int(token_budget_str) if token_budget_str else None,
            default_agent=default_agent,
            debug=os.environ.get("GAMEGUARD_DEBUG_LLM", "0") == "1",
            extra_kwargs=extra,
            disable_thinking=os.environ.get("GAMEGUARD_DISABLE_THINKING", "0") == "1",
        )

    # ---- 主接口 ------------------------------------------------------------

    def chat(
        self,
        messages: list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]] | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        agent: str | None = None,
        tool_choice: str | dict[str, Any] | None = None,
    ) -> LLMResponse:
        """发一轮 LLM 请求；自动走缓存/预算/trace。

        消息格式即 OpenAI chat.completions 标准：
          [{"role": "system", "content": "..."},
           {"role": "user",   "content": "..."},
           {"role": "assistant", "tool_calls": [...]},
           {"role": "tool", "tool_call_id": "...", "content": "..."}]

        tools 结构为 OpenAI function-calling schema（LiteLLM 会按需翻译
        到 Anthropic / Zhipu / ... 各家的专属格式）。

        tool_choice 取值：
          - None / "auto"  : 由模型决定是否调工具（默认）
          - "required"     : 强制本轮必须调用至少一个工具
                             （治推理型模型把 max_tokens 烧在 reasoning_content
                             里 / tool_calls 输出空的"静默推理"问题）
          - "none"         : 禁止本轮调工具
          - {"type": "function", "function": {"name": "x"}}：指定工具
        """
        agent = agent or self.default_agent
        temperature = self.temperature if temperature is None else temperature
        max_tokens = self.max_tokens if max_tokens is None else max_tokens

        cache_payload = {
            "model": self.model,
            "messages": messages,
            "tools": tools or [],
            "temperature": temperature,
            "max_tokens": max_tokens,
            "tool_choice": tool_choice,  # 进 cache key，避免和不同 choice 串
            "disable_thinking": self.disable_thinking,  # 同上
        }
        key = self.cache.make_key(cache_payload)

        # 1) 尝试缓存
        cached = self.cache.get(key, temperature=temperature)
        if cached is not None:
            resp = self._parse_response(cached, cached_flag=True)
            self.trace.emit(
                "llm_cached_hit",
                agent=agent,
                model=self.model,
                cache_key=key[:12],
                prompt_tokens=resp.prompt_tokens,
                completion_tokens=resp.completion_tokens,
                cost_usd=0.0,   # cache 命中不再花钱
                tool_calls=[tc.model_dump() for tc in resp.tool_calls],
                content=resp.content[:2000],
            )
            return resp

        # 2) 先做预算预判（悲观估计：允许至少一轮 max_tokens）
        self._check_budget(pending_tokens=max_tokens)

        # 3) 真正打出去
        if self.debug:
            print(
                f"[LLM] -> {self.model} tools={len(tools or [])} "
                f"messages={len(messages)} tool_choice={tool_choice}"
            )
        # 只在显式给了 tool_choice 时传 —— 部分 provider 对 None 的处理不一致。
        completion_kwargs: dict[str, Any] = dict(
            model=self.model,
            messages=messages,
            tools=tools,
            temperature=temperature,
            max_tokens=max_tokens,
            **self.extra_kwargs,
        )
        if tool_choice is not None:
            completion_kwargs["tool_choice"] = tool_choice
        if self.disable_thinking:
            # 智谱 GLM-4.5+ thinking 模型用 ``thinking={"type":"disabled"}`` 关闭。
            # LiteLLM 会把 extra_body 透传给底层 OpenAI-compatible client。
            existing_extra_body = completion_kwargs.get("extra_body") or {}
            completion_kwargs["extra_body"] = {
                **existing_extra_body,
                "thinking": {"type": "disabled"},
            }
        raw = litellm_completion(**completion_kwargs)

        # LiteLLM 返回的对象既不是 pydantic 也不是纯 dict，用 `.model_dump()` 或
        # 手动转成纯 dict 更稳；以下做法兼容老/新版本。
        raw_dict = _to_plain_dict(raw)

        resp = self._parse_response(raw_dict, cached_flag=False)

        # 4) 记账 + 缓存 + trace
        self.used_tokens += resp.prompt_tokens + resp.completion_tokens
        self.used_usd += resp.cost_usd
        self.cache.put(key, raw_dict, temperature=temperature)
        self.trace.emit(
            "llm_response",
            agent=agent,
            model=self.model,
            cache_key=key[:12],
            prompt_tokens=resp.prompt_tokens,
            completion_tokens=resp.completion_tokens,
            cost_usd=resp.cost_usd,
            tool_calls=[tc.model_dump() for tc in resp.tool_calls],
            content=resp.content[:2000],
        )

        # 5) 事后再核一次预算（本轮已花的钱一定要算进去）
        self._check_budget(pending_tokens=0)
        return resp

    # ---- 内部工具 -----------------------------------------------------------

    def _parse_response(self, raw: dict[str, Any], *, cached_flag: bool) -> LLMResponse:
        """把 LiteLLM 响应 dict 转成 LLMResponse（归一化）。"""
        choices = raw.get("choices") or []
        msg: dict[str, Any] = choices[0].get("message", {}) if choices else {}
        content = msg.get("content") or ""
        tool_calls_raw = msg.get("tool_calls") or []

        tool_calls: list[ToolCall] = []
        for i, tc in enumerate(tool_calls_raw):
            # LiteLLM 里 function.arguments 通常是 JSON 字符串
            fn = tc.get("function") or {}
            args = fn.get("arguments") or "{}"
            if isinstance(args, str):
                import json
                try:
                    args = json.loads(args) if args.strip() else {}
                except json.JSONDecodeError:
                    # LLM 偶尔会产出坏 JSON；不在这里 crash，交给 AgentLoop
                    # 把原始字符串作为 "_raw" 传下去，让 tool dispatcher 走
                    # 错误反馈路径。
                    args = {"_raw": args, "_parse_error": True}
            tool_calls.append(
                ToolCall(
                    id=tc.get("id") or f"call_{i}",
                    name=fn.get("name") or "",
                    arguments=args if isinstance(args, dict) else {},
                )
            )

        usage = raw.get("usage") or {}
        cost = (
            raw.get("response_cost")                           # LiteLLM 顶层字段
            or (raw.get("_hidden_params") or {}).get("response_cost")
            or 0.0
        )
        return LLMResponse(
            model=raw.get("model") or self.model,
            content=content,
            tool_calls=tool_calls,
            prompt_tokens=int(usage.get("prompt_tokens") or 0),
            completion_tokens=int(usage.get("completion_tokens") or 0),
            cost_usd=float(cost or 0.0),
            cached=cached_flag,
            raw=raw,
        )

    def _check_budget(self, *, pending_tokens: int) -> None:
        if self.usd_budget is not None and self.used_usd > self.usd_budget:
            raise BudgetExceeded(
                f"USD 预算 ${self.usd_budget:.3f} 已用尽（已花 ${self.used_usd:.4f}）"
            )
        if self.token_budget is not None:
            if self.used_tokens + pending_tokens > self.token_budget:
                raise BudgetExceeded(
                    f"token 预算 {self.token_budget} 将被超出"
                    f"（已用 {self.used_tokens}，悲观估计还要 {pending_tokens}）"
                )

# --------------------------------------------------------------------------- #
# 工具函数
# --------------------------------------------------------------------------- #

def _to_plain_dict(obj: Any) -> dict[str, Any]:
    """把 LiteLLM 返回对象转成纯 dict。兼容不同版本。"""
    if isinstance(obj, dict):
        return obj
    if hasattr(obj, "model_dump"):
        return obj.model_dump()
    if hasattr(obj, "dict"):
        return obj.dict()
    if hasattr(obj, "__dict__"):
        return dict(obj.__dict__)
    raise TypeError(f"无法把 {type(obj)!r} 转成 dict")

# 让 LiteLLM 在 debug 环境下打印更多信息；面试录 demo 时方便复盘
if os.environ.get("GAMEGUARD_DEBUG_LLM") == "1":
    litellm.set_verbose = True  # type: ignore[attr-defined]

# --------------------------------------------------------------------------- #
# Provider 映射：把项目内部统一语法翻译成 LiteLLM 认识的形式
# --------------------------------------------------------------------------- #

# 各 provider 的 OpenAI-compatible endpoint + api-key 环境变量映射。
# 只列当前项目可能会用的；将来要接 DeepSeek / MoonShot / 月之暗面 / 阶跃星辰
# 等在这里加一行即可。
# 把非 LiteLLM 原生 provider 映射到 OpenAI-compatible endpoint。
# 当前空 —— zai/ 已经被 LiteLLM 原生识别。保留这个机制只是为了将来
# 接入别的国产模型时不用再改结构。
_PROVIDER_MAP: dict[str, dict[str, str]] = {}

def _resolve_provider(model: str) -> tuple[str, dict[str, Any]]:
    """根据 ``provider/model`` 语法决定传给 LiteLLM 的参数。

    三种分支：
      1) provider 在 ``_PROVIDER_MAP`` 中：走 OpenAI-compatible 适配，返回
         ``("openai/<rest>", {api_base, api_key})``。
      2) provider 是 ``zai``：LiteLLM 原生支持；如果用户在 env 里写了
         ``ZHIPU_API_BASE``，覆盖默认的 z.ai 海外 endpoint 为智谱国内站。
      3) 其它：原样返回，让 LiteLLM 路由。
    """
    if "/" not in model:
        return model, {}

    provider, rest = model.split("/", 1)

    mapping = _PROVIDER_MAP.get(provider)
    if mapping is not None:
        api_key = os.environ.get(mapping["api_key_env"])
        extra: dict[str, Any] = {"api_base": mapping["api_base"]}
        if api_key:
            extra["api_key"] = api_key
        return f"openai/{rest}", extra

    # zai 的可选覆盖：智谱国内站
    if provider == "zai":
        extra = {}
        base_override = os.environ.get("ZHIPU_API_BASE")
        if base_override:
            extra["api_base"] = base_override
        # ZHIPU_API_KEY 与 ZAI_API_KEY 二选一：兼容用户从 bigmodel.cn 拿到的 key
        key = os.environ.get("ZAI_API_KEY") or os.environ.get("ZHIPU_API_KEY")
        if key:
            extra["api_key"] = key
        return model, extra

    return model, {}
