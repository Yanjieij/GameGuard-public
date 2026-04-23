"""把 Python 函数变成 LLM 能调的 tool。

LangChain / LlamaIndex 都有现成的 tool 抽象，这里选择手写——一是不想只是
在调 LangChain，二是想对 function-calling 协议有直接理解。

文件做三件事：

  1. 每个 tool 的输入由一个 Pydantic 模型定义。这样能自动获得类型检查、
     默认值、描述字段，并一键导出成 OpenAI function-calling schema。
  2. 每个 tool 的输出序列化成 JSON 回传给 LLM。tool result 会作为下一轮
     LLM 输入的一部分，必须稳定可序列化。
  3. 提供一个 ToolRegistry：按名字查找、统一派发、把异常转成结构化错误
     反馈而不是 Python traceback。这是让 LLM 能自修复的关键——ReAct 论文
     指出"让模型看到错误消息能显著提升恢复率"。

OpenAI function-calling schema 长这样：

    [
      {
        "type": "function",
        "function": {
          "name": "read_file",
          "description": "读取一个文件的完整内容。",
          "parameters": {
            "type": "object",
            "properties": {
              "path": {"type": "string", "description": "相对项目根的路径"}
            },
            "required": ["path"]
          }
        }
      }
    ]

Anthropic / 智谱 / DeepSeek 各家内部 schema 有差，LiteLLM 会自动转，我们
只生成 OpenAI 版本就够。Pydantic v2 自带 model_json_schema() 可以直接吐出
parameters 那一块。
"""
from __future__ import annotations

import json
import traceback
from dataclasses import dataclass, field
from typing import Any, Callable, Generic, TypeVar

from pydantic import BaseModel, ValidationError

# --------------------------------------------------------------------------- #
# 类型定义
# --------------------------------------------------------------------------- #

InputT = TypeVar("InputT", bound=BaseModel)
OutputT = TypeVar("OutputT")

@dataclass
class Tool(Generic[InputT, OutputT]):
    """单个 tool 的定义。

    - name:        tool 在 LLM 视角下的名字（函数名风格：snake_case）
    - description: LLM 看到的描述；写得好不好直接影响工具选择质量
    - input_model: Pydantic 类型，参数的 schema
    - fn:          实现函数，签名 ``(parsed_input) -> output``
    """

    name: str
    description: str
    input_model: type[InputT]
    fn: Callable[[InputT], OutputT]
    # 输出序列化钩子：默认尝试直接 json.dumps；对象需要自定义时可传 override。
    output_serializer: Callable[[OutputT], Any] | None = None

    def schema(self) -> dict[str, Any]:
        """生成 OpenAI-compatible function schema。"""
        params = self.input_model.model_json_schema()
        # pydantic 默认包含 title / $defs，tools schema 里是允许保留的；保留便于 debug
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": params,
            },
        }

# --------------------------------------------------------------------------- #
# 工具执行的统一结果（暴露给 AgentLoop）
# --------------------------------------------------------------------------- #

class ToolInvocationResult(BaseModel):
    """一次 tool 调用的结果。

    我们不 raise：所有错误（schema 不匹配、函数抛错、输出不能序列化）
    都打包成 ok=False 的结果回到 AgentLoop，由它 append 到 messages 里
    让 LLM 自己看到并修复。这是 ReAct 论文里反复强调的"反馈闭环"。
    """

    ok: bool
    tool_name: str
    # 序列化后的内容（str 形式），直接塞进 role=tool 的 content
    content: str
    # 便于 trace/debug 的结构化副本
    payload: Any = None
    error: str | None = None
    error_kind: str | None = None  # "schema" / "runtime" / "serialize"

# --------------------------------------------------------------------------- #
# 注册表
# --------------------------------------------------------------------------- #

@dataclass
class ToolRegistry:
    """把若干 Tool 注册到一起，统一派发。"""

    tools: dict[str, Tool] = field(default_factory=dict)

    def register(self, tool: Tool) -> None:
        if tool.name in self.tools:
            raise ValueError(f"tool {tool.name!r} 已注册")
        self.tools[tool.name] = tool

    def register_many(self, tools: list[Tool]) -> None:
        for t in tools:
            self.register(t)

    # ---- AgentLoop 直接用的接口 -----------------------------------------

    def to_openai_schema(self) -> list[dict[str, Any]]:
        return [t.schema() for t in self.tools.values()]

    def dispatch(self, name: str, arguments: dict[str, Any]) -> ToolInvocationResult:
        """按名字调度一个 tool。永远不 raise。"""
        tool = self.tools.get(name)
        if tool is None:
            return ToolInvocationResult(
                ok=False,
                tool_name=name,
                content=f"ERROR: tool {name!r} 未注册。可用 tool: {list(self.tools)}",
                error=f"unknown tool {name!r}",
                error_kind="schema",
            )

        # 1) schema 校验
        try:
            # LLM 偶尔在 arguments 顶层塞进 _raw / _parse_error（client.py 的
            # 兜底路径），显式拒绝而不是硬塞到 model_validate 里。
            if "_parse_error" in arguments:
                raise ValidationError.from_exception_data("ToolInput", [])
            parsed = tool.input_model.model_validate(arguments)
        except ValidationError as e:
            return ToolInvocationResult(
                ok=False,
                tool_name=name,
                content=f"ERROR: 参数不满足 schema:\n{e.errors()}",
                error=str(e),
                error_kind="schema",
            )
        except Exception as e:  # noqa: BLE001
            return ToolInvocationResult(
                ok=False,
                tool_name=name,
                content=f"ERROR: 参数解析失败: {type(e).__name__}: {e}",
                error=str(e),
                error_kind="schema",
            )

        # 2) 执行
        try:
            output = tool.fn(parsed)
        except Exception as e:  # noqa: BLE001
            tb = traceback.format_exc(limit=3)
            return ToolInvocationResult(
                ok=False,
                tool_name=name,
                # 反馈给 LLM 时脱敏：只保留异常类 + 消息，不吐完整 traceback
                # 否则 prompt 会被调用栈污染，且暴露过多内部路径
                content=f"ERROR: tool 执行失败: {type(e).__name__}: {e}",
                error=tb,
                error_kind="runtime",
            )

        # 3) 序列化
        try:
            if tool.output_serializer is not None:
                serial = tool.output_serializer(output)
            else:
                serial = _default_serialize(output)
            # 统一转成 str 后塞给 LLM
            content_str = serial if isinstance(serial, str) else json.dumps(
                serial, ensure_ascii=False, default=str
            )
        except Exception as e:  # noqa: BLE001
            return ToolInvocationResult(
                ok=False,
                tool_name=name,
                content=f"ERROR: tool 返回无法序列化: {e}",
                payload=output,
                error=str(e),
                error_kind="serialize",
            )

        return ToolInvocationResult(
            ok=True,
            tool_name=name,
            content=content_str,
            payload=output,
        )

# --------------------------------------------------------------------------- #
# 默认序列化器
# --------------------------------------------------------------------------- #

def _default_serialize(obj: Any) -> Any:
    """把 tool 返回值转成"能喂给 LLM 的东西"。

    - Pydantic 模型：`.model_dump(mode="json")` 返回 dict
    - dict/list：原样（等 json.dumps 处理）
    - 字符串：原样
    - 其它：尝试 `str(obj)`
    """
    if isinstance(obj, BaseModel):
        return obj.model_dump(mode="json")
    if isinstance(obj, (dict, list, str, int, float, bool)) or obj is None:
        return obj
    # 列表里可能混 Pydantic
    if hasattr(obj, "__iter__"):
        try:
            return [_default_serialize(x) for x in obj]
        except TypeError:
            pass
    return str(obj)
