"""D4 meta-tests —— Agent 层骨架不打实 LLM 也能验证。

==============================================================================
为什么必须写这类 mock 测试？
==============================================================================

直接跑真 LLM 才发现 AgentLoop 有 bug，代价是"钱 + 时间 + debug 难度"。
业界 agent eval 的一条铁律："先用 mock 打通框架，再上真模型"。这里的
``_MockLLMClient`` 按预设脚本返回 LLM 响应，和 AgentLoop 对接起来等价
一条"没有花钱的完整流程测试"。

覆盖：
  - ToolRegistry 的 ok / schema-error / runtime-error 三态
  - doc_tools 的 outline 与 section 读取
  - LLMCache 的 put/get/miss
  - AgentLoop 的 tool-calling 循环（多轮、LLM 自修复）
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel, Field

from gameguard.agents.base import AgentLoop
from gameguard.llm.cache import CacheMissInStrictMode, LLMCache
from gameguard.llm.client import LLMResponse, ToolCall
from gameguard.llm.trace import LLMTrace
from gameguard.tools.doc_tools import (
    DocRepository,
    build_doc_tools,
    _extract_outline,
    _read_one_section,
)
from gameguard.tools.schemas import Tool, ToolRegistry


# =========================================================================== #
# 1. LLMCache
# =========================================================================== #


def test_llm_cache_put_get_miss(tmp_path: Path) -> None:
    cache = LLMCache(root=tmp_path)
    payload = {"model": "m", "messages": [{"role": "user", "content": "hi"}], "tools": []}
    key = cache.make_key(payload)

    # miss 时返回 None
    assert cache.get(key, temperature=0.0) is None
    assert cache.misses == 1

    # put 后命中
    cache.put(key, {"answer": 42}, temperature=0.0)
    got = cache.get(key, temperature=0.0)
    assert got == {"answer": 42}
    assert cache.hits == 1


def test_llm_cache_bypasses_random_temperature(tmp_path: Path) -> None:
    cache = LLMCache(root=tmp_path)
    key = cache.make_key({"x": 1})
    assert cache.get(key, temperature=0.7) is None
    assert cache.bypassed == 1
    # 即使 put 了也不应该写入（temperature > 0）
    cache.put(key, {"x": 1}, temperature=0.7)
    # 重新用 deterministic 查，仍是 miss
    assert cache.get(key, temperature=0.0) is None


def test_llm_cache_strict_mode_raises(tmp_path: Path) -> None:
    cache = LLMCache(root=tmp_path, strict=True)
    key = cache.make_key({"x": 1})
    with pytest.raises(CacheMissInStrictMode):
        cache.get(key, temperature=0.0)


# =========================================================================== #
# 2. Tool schemas / registry
# =========================================================================== #


class _AddInput(BaseModel):
    x: int = Field(..., description="left")
    y: int = Field(..., description="right")


class _AddOutput(BaseModel):
    sum: int


def _add(i: _AddInput) -> _AddOutput:
    return _AddOutput(sum=i.x + i.y)


def _boom(_: _AddInput) -> _AddOutput:  # noqa: ARG001
    raise RuntimeError("kaboom")


def test_tool_registry_dispatch_ok() -> None:
    reg = ToolRegistry()
    reg.register(
        Tool(name="add", description="add two ints", input_model=_AddInput, fn=_add)
    )
    result = reg.dispatch("add", {"x": 3, "y": 4})
    assert result.ok is True
    assert json.loads(result.content) == {"sum": 7}
    # schema 对外导出：LLM 能看到 parameters 字段
    schema = reg.to_openai_schema()[0]
    assert schema["function"]["name"] == "add"
    assert "properties" in schema["function"]["parameters"]


def test_tool_registry_dispatch_schema_error() -> None:
    reg = ToolRegistry()
    reg.register(Tool(name="add", description="d", input_model=_AddInput, fn=_add))
    result = reg.dispatch("add", {"x": "not-an-int"})
    assert result.ok is False
    assert result.error_kind == "schema"
    assert "ERROR" in result.content


def test_tool_registry_dispatch_runtime_error() -> None:
    reg = ToolRegistry()
    reg.register(Tool(name="boom", description="d", input_model=_AddInput, fn=_boom))
    result = reg.dispatch("boom", {"x": 1, "y": 2})
    assert result.ok is False
    assert result.error_kind == "runtime"
    assert "kaboom" in result.content


def test_tool_registry_unknown_tool() -> None:
    reg = ToolRegistry()
    result = reg.dispatch("nope", {})
    assert result.ok is False
    assert result.error_kind == "schema"


# =========================================================================== #
# 3. Doc tools
# =========================================================================== #


SAMPLE_MD = """\
# Root

intro line

## Section A

body A line 1
body A line 2

### A.1

sub body

## Section B

body B
"""


def test_extract_outline_basic() -> None:
    outline = _extract_outline(SAMPLE_MD)
    headings = [(s.heading, s.level) for s in outline]
    assert headings == [
        ("Root", 1),
        ("Section A", 2),
        ("A.1", 3),
        ("Section B", 2),
    ]


def test_read_section_includes_subsections() -> None:
    sec = _read_one_section(SAMPLE_MD, "Section A", include_subsections=True)
    assert sec is not None
    # 必须包含 A.1 子节
    assert "A.1" in sec["content"]


def test_read_section_excludes_subsections() -> None:
    sec = _read_one_section(SAMPLE_MD, "Section A", include_subsections=False)
    assert sec is not None
    assert "A.1" not in sec["content"]


def test_doc_repository_registers_real_file(tmp_path: Path) -> None:
    p = tmp_path / "demo.md"
    p.write_text(SAMPLE_MD, encoding="utf-8")
    repo = DocRepository()
    name = repo.register_file(p)
    assert name == "demo"
    assert "Root" in repo.get(name)
    tools = build_doc_tools(repo)
    # 至少 4 个工具（list_docs / list_doc_sections / read_doc_section / read_full_doc）
    assert {t.name for t in tools} == {
        "list_docs",
        "list_doc_sections",
        "read_doc_section",
        "read_full_doc",
    }


# =========================================================================== #
# 4. AgentLoop 与 MockLLMClient 的联调
# =========================================================================== #


@dataclass
class _MockLLMClient:
    """按预设脚本逐轮返回响应的 LLMClient 替身。

    AgentLoop 只读 client.chat / client.trace 两个接口；我们实现最小子集。
    """

    scripted: list[LLMResponse]
    trace: LLMTrace
    _cursor: int = 0
    _calls: list[list[dict[str, Any]]] = field(default_factory=list)

    def chat(self, messages, *, tools=None, temperature=None, max_tokens=None, agent=None, tool_choice=None):  # noqa: ARG002
        self._calls.append(list(messages))
        if self._cursor >= len(self.scripted):
            raise AssertionError("mock 脚本耗尽：AgentLoop 请求了超出预期的 LLM 轮次")
        resp = self.scripted[self._cursor]
        self._cursor += 1
        return resp


def _echo_tool() -> Tool:
    class _In(BaseModel):
        text: str

    class _Out(BaseModel):
        echo: str

    def _fn(i: _In) -> _Out:
        return _Out(echo=i.text.upper())

    return Tool(name="echo", description="echo uppercase", input_model=_In, fn=_fn)


def test_agent_loop_completes_on_no_tool_calls(tmp_path: Path) -> None:
    trace = LLMTrace(path=tmp_path / "trace.jsonl", session_id="t")
    client = _MockLLMClient(
        scripted=[
            LLMResponse(model="mock", content="all done", tool_calls=[]),
        ],
        trace=trace,
    )
    reg = ToolRegistry()
    reg.register(_echo_tool())
    loop = AgentLoop(
        client=client,  # type: ignore[arg-type]  mock 够鸭子
        tools=reg,
        agent_name="T",
        system_prompt="sys",
    )
    loop.add_user_message("go")
    stats = loop.run()

    assert stats.stopped_reason == "no_tool_calls"
    assert stats.final_content == "all done"
    assert stats.steps == 1


def test_agent_loop_dispatches_tool_and_continues(tmp_path: Path) -> None:
    """LLM 先请求调用 echo，然后再一轮结束。"""
    trace = LLMTrace(path=tmp_path / "trace.jsonl", session_id="t")
    client = _MockLLMClient(
        scripted=[
            LLMResponse(
                model="mock",
                content="",
                tool_calls=[ToolCall(id="c1", name="echo", arguments={"text": "hello"})],
            ),
            LLMResponse(model="mock", content="finished", tool_calls=[]),
        ],
        trace=trace,
    )
    reg = ToolRegistry()
    reg.register(_echo_tool())
    loop = AgentLoop(client=client, tools=reg, agent_name="T", system_prompt="s")  # type: ignore[arg-type]
    loop.add_user_message("do echo")
    stats = loop.run()

    assert stats.stopped_reason == "no_tool_calls"
    assert stats.tool_calls == 1
    # 第二轮 messages 里应该有 role=tool 的返回（内容是 JSON，含 "HELLO"）
    second_call_messages = client._calls[1]
    tool_msg = [m for m in second_call_messages if m.get("role") == "tool"][0]
    assert "HELLO" in tool_msg["content"]


def test_agent_loop_recovers_from_tool_error(tmp_path: Path) -> None:
    """LLM 先传坏参数 → tool 返回 schema 错误 → LLM 在第二轮修正。"""
    trace = LLMTrace(path=tmp_path / "trace.jsonl", session_id="t")
    client = _MockLLMClient(
        scripted=[
            LLMResponse(
                model="mock",
                content="",
                tool_calls=[ToolCall(id="c1", name="echo", arguments={"bad": "field"})],
            ),
            LLMResponse(
                model="mock",
                content="",
                tool_calls=[ToolCall(id="c2", name="echo", arguments={"text": "x"})],
            ),
            LLMResponse(model="mock", content="ok", tool_calls=[]),
        ],
        trace=trace,
    )
    reg = ToolRegistry()
    reg.register(_echo_tool())
    loop = AgentLoop(client=client, tools=reg, agent_name="T", system_prompt="s")  # type: ignore[arg-type]
    loop.add_user_message("go")
    stats = loop.run()

    assert stats.tool_calls == 2
    assert stats.tool_failures == 1
    assert stats.stopped_reason == "no_tool_calls"
    assert stats.steps == 3


def test_agent_loop_stops_on_max_steps(tmp_path: Path) -> None:
    """LLM 一直请求 tool；loop 在 max_steps 处退出。"""
    trace = LLMTrace(path=tmp_path / "trace.jsonl", session_id="t")

    def _loop_call() -> LLMResponse:
        return LLMResponse(
            model="mock",
            content="",
            tool_calls=[ToolCall(id="x", name="echo", arguments={"text": "x"})],
        )

    client = _MockLLMClient(scripted=[_loop_call() for _ in range(10)], trace=trace)
    reg = ToolRegistry()
    reg.register(_echo_tool())
    loop = AgentLoop(client=client, tools=reg, agent_name="T", system_prompt="s", max_steps=3)  # type: ignore[arg-type]
    loop.add_user_message("go")
    stats = loop.run()

    assert stats.stopped_reason == "max_steps"
    assert stats.steps == 3


# =========================================================================== #
# 5. TestGenAgent discovery 模式（恢复后默认行为）
# =========================================================================== #


def test_testgen_discovery_mode_calls_list_tools(tmp_path: Path) -> None:
    """
    Plan A 验收：discovery 模式下，TestGenAgent 必须真的调用 list_invariants
    / list_skills / list_characters 三个只读工具，再 emit_testcase × N + finalize。

    此测试用 mock LLM 严格对脚本，验证：
      1. 工具调用顺序符合 system prompt 描述的工作流
      2. emit_testcase 拿到的 invariant_id 来自 list_invariants 的返回
         （证明 list_* 不再是死工具）
      3. finalize 收尾后立即停止（stop_when 生效）
    """
    from gameguard.agents.test_gen import run_test_gen_agent
    from gameguard.domain.character import Character
    from gameguard.domain.invariant import HpNonnegInvariant, InvariantBundle
    from gameguard.domain.skill import DamageType, SkillBook, SkillSpec

    # ---- 准备最小可用的输入数据 ----
    bundle = InvariantBundle(
        items=[
            HpNonnegInvariant(id="I-01-p1", description="p1 hp 非负", actor="p1"),
        ]
    )
    skill_book = SkillBook()
    skill_book.register(
        SkillSpec(
            id="skill_test",
            name="Test",
            mp_cost=10,
            cast_time=0.5,
            cooldown=2.0,
            damage_base=10,
            damage_type=DamageType.PHYSICAL,
        )
    )
    characters = [
        Character(id="p1", name="P", hp=100, hp_max=100, mp=50, mp_max=50),
        Character(id="dummy", name="D", hp=100, hp_max=100, mp=0, mp_max=0),
    ]

    # ---- 构造 mock LLM 脚本 ----
    # 三轮只读 tool 调用 + 一轮 emit + finalize 并行 + 收尾轮（实际不会触发，stop_when 拦截）
    scripted = [
        # 轮 1：list_invariants
        LLMResponse(
            model="mock", content="先看不变式。",
            tool_calls=[ToolCall(id="c1", name="list_invariants", arguments={})],
        ),
        # 轮 2：list_skills
        LLMResponse(
            model="mock", content="再看技能。",
            tool_calls=[ToolCall(id="c2", name="list_skills", arguments={})],
        ),
        # 轮 3：list_characters
        LLMResponse(
            model="mock", content="再看角色。",
            tool_calls=[ToolCall(id="c3", name="list_characters", arguments={})],
        ),
        # 轮 4：emit_testcase + finalize（parallel）
        LLMResponse(
            model="mock", content="开始 emit。",
            tool_calls=[
                ToolCall(
                    id="c4",
                    name="emit_testcase",
                    arguments={
                        "id": "smoke-p1-hp",
                        "name": "p1 HP 烟雾测试",
                        "description": "释放一次 skill_test，验证 p1 hp 仍非负",
                        "tags": ["smoke"],
                        "derived_from": ["invariant:I-01-p1"],
                        "seed": 1,
                        "sandbox": "pysim:v1",
                        "actions": [
                            {"kind": "cast", "actor": "p1", "skill": "skill_test", "target": "dummy"},
                            {"kind": "wait", "seconds": 1.0},
                        ],
                        "assertion_invariant_ids": ["I-01-p1"],
                    },
                ),
                ToolCall(id="c5", name="finalize", arguments={"reason": "done"}),
            ],
        ),
    ]
    trace = LLMTrace(path=tmp_path / "tg_trace.jsonl", session_id="tg")
    mock_client = _MockLLMClient(scripted=scripted, trace=trace)

    # ---- 跑 TestGenAgent，discovery 模式（默认） ----
    result = run_test_gen_agent(
        bundle=bundle,
        skill_book=skill_book,
        initial_characters=characters,
        llm=mock_client,  # type: ignore[arg-type]  鸭子类型
        max_steps=10,
        prefetch_context=False,   # discovery 模式
        tool_choice=None,         # auto，DeepSeek 推荐
    )

    # ---- 断言 ----
    assert result.stats.steps == 4, f"应当 4 轮收敛（3 list + 1 emit/finalize），实际 {result.stats.steps}"
    assert result.stats.stopped_reason == "stop_when", "应当被 stop_when（finalize 触发）截停"
    assert result.finalized_by_agent is True
    assert len(result.plan.cases) == 1
    case = result.plan.cases[0]
    assert case.id == "smoke-p1-hp"
    assert len(case.assertions) == 1
    assert case.assertions[0].invariant.id == "I-01-p1"

    # 关键：list_* 工具确实被调用了（不是死工具）
    second_round_msgs = mock_client._calls[1]
    tool_msgs = [m for m in second_round_msgs if m.get("role") == "tool"]
    # 第二轮入参里应该包含第一轮 list_invariants 的工具响应
    assert any("I-01-p1" in (m.get("content") or "") for m in tool_msgs), \
        "list_invariants 的返回应被作为 tool message 喂回 LLM"


def test_testgen_prefetch_mode_skips_list_tools(tmp_path: Path) -> None:
    """
    Plan A 验收：prefetch 模式（GLM-4.7 fallback）下，user message 应该
    包含静态上下文，且 LLM 不需要调 list_* 工具。
    """
    from gameguard.agents.test_gen import _build_prefetched_task_message
    from gameguard.domain.character import Character
    from gameguard.domain.invariant import HpNonnegInvariant, InvariantBundle
    from gameguard.domain.skill import DamageType, SkillBook, SkillSpec

    bundle = InvariantBundle(
        items=[HpNonnegInvariant(id="I-01-p1", description="hp", actor="p1")]
    )
    skill_book = SkillBook()
    skill_book.register(
        SkillSpec(
            id="s1", name="S1", mp_cost=10, cast_time=0.5, cooldown=2.0,
            damage_base=5, damage_type=DamageType.PHYSICAL,
        )
    )
    characters = [Character(id="p1", name="P", hp=100, hp_max=100, mp=50, mp_max=50)]

    msg = _build_prefetched_task_message(bundle, skill_book, characters)
    # invariants / skills / characters 静态上下文必须在消息里
    assert "I-01-p1" in msg
    assert "s1" in msg
    assert "p1" in msg
    assert "list_invariants" not in msg or "已经把" in msg, \
        "prefetch 模式不应让 LLM 自己再调 list_invariants"
