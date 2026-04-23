"""手写的 Agent tool-calling 循环。

Agent 本质上就是个 while 循环：把消息发给 LLM，LLM 回一批工具调用，
把结果塞回去再问一次，直到它不再想调工具为止。

    while LLM 仍然想调 tool:
        1) 把 messages 发给 LLM
        2) LLM 返回 (content, tool_calls)
        3) 对每个 tool_call：查注册表、校验参数、执行、把结果回塞
    返回最后一次的 content，或由某个 tool 通过 side channel 提交的结构化成品

难写的是边界情况：
  - 达到步数上限怎么退出，防死循环
  - 工具失败怎么反馈给 LLM，让它修正后重试
  - prompt / 响应怎么进 trace
  - 一轮返回多个 tool_call 怎么调度（OpenAI 和 LiteLLM 都支持并行 tool_calls）

下面这版 ~200 行覆盖了这些点。不用 LangChain / LangGraph，是因为手写
能让边界行为全在自己手里，出问题直接 debug。

这个 loop 本身不区分 ReAct 和 plan-and-execute，两者的差别在 prompt 和
tool 集合上：
  - ReAct：prompt 鼓励 LLM 交替输出 "Thought -> Action -> Observation"，
    tools 是小而多的交互式原语（read / step）。
  - plan-and-execute：第一轮让 LLM 只 emit 一份 plan（结构化 JSON），
    后续轮再执行和复盘，tools 分两类（plan 阶段的 emit 工具 / execute 阶段的原语）。

DesignDocAgent 天然适合 plan-and-execute（看目录 → 读关键节 → 一次性 emit
所有 invariant）。TestGenAgent 也可以分阶段。loop 不绑定策略是好事。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel

from typing import Callable

from gameguard.llm.client import LLMClient, LLMResponse
from gameguard.tools.schemas import ToolInvocationResult, ToolRegistry


# --------------------------------------------------------------------------- #
# 运行结果
# --------------------------------------------------------------------------- #


class AgentRunStats(BaseModel):
    """一次 AgentLoop.run() 的运行统计。"""

    steps: int = 0
    tool_calls: int = 0
    tool_failures: int = 0
    final_content: str = ""
    stopped_reason: str = ""    # "no_tool_calls" / "max_steps" / "budget" / "error"


@dataclass
class AgentLoop:
    """一个可被多个 Agent 复用的 tool-calling 主循环。"""

    # ---- 依赖 ----
    client: LLMClient
    tools: ToolRegistry
    # ---- 配置 ----
    agent_name: str = "Agent"
    system_prompt: str = ""
    max_steps: int = 20
    # 初始用户消息（通常是任务说明）；后续轮 run() 里追加
    messages: list[dict[str, Any]] = field(default_factory=list)
    # 传 "required" 可以强制 LLM 每轮都调工具。推理型模型（GLM-4.7、Claude
    # thinking、o1）在 tool-calling 时偶尔会把 max_tokens 全花在内部推理上，
    # 结果 tool_calls=[] 空数组，陷入静默卡死。强制 required 能消除这个失败模式。
    tool_choice: str | dict[str, Any] | None = None
    # tool 执行后的收敛回调：返回 True 就立即退出。常见用法是
    # lambda r: r.ok and r.tool_name == "finalize"。配 tool_choice="required"
    # 必须要加这个——否则 finalize 之后 LLM 还会被强制再调一次工具。
    stop_when: Callable[[ToolInvocationResult], bool] | None = None

    # --------------------------------------------------------------------- #
    # 初始化 messages
    # --------------------------------------------------------------------- #

    def __post_init__(self) -> None:
        # 系统消息放第一条。后续 add_user_message / run 都不碰它。
        if self.system_prompt and not self.messages:
            self.messages.append({"role": "system", "content": self.system_prompt})
        elif self.system_prompt and self.messages[0].get("role") != "system":
            self.messages.insert(0, {"role": "system", "content": self.system_prompt})

    def add_user_message(self, content: str) -> None:
        self.messages.append({"role": "user", "content": content})

    # --------------------------------------------------------------------- #
    # 主循环
    # --------------------------------------------------------------------- #

    def run(self) -> AgentRunStats:
        """跑 tool-calling 循环直到 LLM 不再要求 tool 或达到步数上限。"""
        stats = AgentRunStats()
        tool_schemas = self.tools.to_openai_schema()

        for step in range(1, self.max_steps + 1):
            stats.steps = step
            self.client.trace.emit(
                "agent_step_start",
                agent=self.agent_name,
                step_index=step,
                messages_len=len(self.messages),
            )

            resp: LLMResponse = self.client.chat(
                messages=self.messages,
                tools=tool_schemas,
                agent=self.agent_name,
                tool_choice=self.tool_choice,
            )

            # 先把 assistant 的消息塞进 messages。OpenAI 协议规定 tool 响应
            # 必须紧跟在发起它的 assistant 消息后面，否则会报序列错误。
            self.messages.append(_assistant_message(resp))

            if resp.finished:
                stats.final_content = resp.content
                stats.stopped_reason = "no_tool_calls"
                self.client.trace.emit(
                    "agent_stop",
                    agent=self.agent_name,
                    reason=stats.stopped_reason,
                    final_content=resp.content[:500],
                )
                return stats

            # 有 tool_calls：逐个执行、把结果回塞
            for tc in resp.tool_calls:
                stats.tool_calls += 1
                result = self.tools.dispatch(tc.name, tc.arguments)
                if not result.ok:
                    stats.tool_failures += 1

                self.client.trace.emit(
                    "tool_result",
                    agent=self.agent_name,
                    tool=tc.name,
                    tool_call_id=tc.id,
                    arguments=tc.arguments,
                    ok=result.ok,
                    error_kind=result.error_kind,
                    # content 截断，避免 trace 文件过大
                    content=result.content[:2000],
                )

                self.messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tc.id,
                        "content": result.content,
                    }
                )

                # 典型场景：LLM 调了 finalize，我们要立刻退出，否则下一轮
                # 又会被 tool_choice="required" 逼着多调一次工具。
                if self.stop_when is not None and self.stop_when(result):
                    stats.final_content = resp.content
                    stats.stopped_reason = "stop_when"
                    self.client.trace.emit(
                        "agent_stop",
                        agent=self.agent_name,
                        reason=stats.stopped_reason,
                        triggered_by_tool=result.tool_name,
                    )
                    return stats

        # 跑满步数上限还没收敛，通常是 agent 卡在循环里或 prompt 没说清退出条件。
        stats.stopped_reason = "max_steps"
        self.client.trace.emit(
            "agent_stop",
            agent=self.agent_name,
            reason=stats.stopped_reason,
            max_steps=self.max_steps,
        )
        return stats


# --------------------------------------------------------------------------- #
# 工具函数
# --------------------------------------------------------------------------- #


def _assistant_message(resp: LLMResponse) -> dict[str, Any]:
    """把 LLMResponse 转成 OpenAI chat.completions 风格的 assistant 消息。

    tool_calls 里必须包含 id、type=function、function.{name, arguments}。
    arguments 是 JSON 字符串——我们前面已经解析成 dict 了，这里得重新
    序列化回字符串给协议用。
    """
    msg: dict[str, Any] = {"role": "assistant"}
    if resp.content:
        msg["content"] = resp.content
    if resp.tool_calls:
        msg["tool_calls"] = [
            {
                "id": tc.id,
                "type": "function",
                "function": {
                    "name": tc.name,
                    "arguments": json.dumps(tc.arguments, ensure_ascii=False),
                },
            }
            for tc in resp.tool_calls
        ]
    # OpenAI 要求 content 字段存在，为空时填 null；有些实现容忍缺省
    msg.setdefault("content", None)
    return msg


def summarize_tool_result(r: ToolInvocationResult) -> str:
    """trace/日志里的 tool 结果简报。"""
    badge = "✓" if r.ok else "✗"
    return f"{badge} {r.tool_name} -> {r.content[:120]}"
