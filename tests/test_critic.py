"""D10 meta-tests —— CriticAgent 静态校验 + 决策路径。"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from gameguard.agents.critic import run_critic_agent
from gameguard.domain import (
    CastAction,
    Character,
    InterruptAction,
    WaitAction,
)
from gameguard.domain.invariant import HpNonnegInvariant
from gameguard.domain.skill import DamageType, SkillBook, SkillSpec
from gameguard.llm.client import LLMResponse, ToolCall
from gameguard.llm.trace import LLMTrace
from gameguard.testcase.model import (
    Assertion,
    AssertionWhen,
    TestCase,
    TestPlan,
)
from gameguard.tools.critic_tools import static_check_case


# --------------------------------------------------------------------------- #
# 静态校验单元测试
# --------------------------------------------------------------------------- #


def _build_skill_book() -> SkillBook:
    book = SkillBook()
    book.register(SkillSpec(
        id="fireball", name="Fireball", mp_cost=30, cast_time=1.0,
        cooldown=8.0, damage_base=50, damage_type=DamageType.FIRE,
    ))
    book.register(SkillSpec(
        id="frostbolt", name="Frostbolt", mp_cost=25, cast_time=1.5,
        cooldown=6.0, damage_base=40, damage_type=DamageType.FROST,
    ))
    return book


def _chars() -> list[Character]:
    return [
        Character(id="p1", name="P", hp=500, hp_max=500, mp=100, mp_max=100),
        Character(id="dummy", name="D", hp=1000, hp_max=1000, mp=0, mp_max=0),
    ]


def test_static_check_detects_insufficient_mp() -> None:
    """4 次 fireball 间隔 8.5s（避开 CD），100 MP 不够 4 × 30 = 120。"""
    case = TestCase(
        id="bad-mp", name="bad-mp", seed=1, sandbox="pysim:v1",
        actions=[
            CastAction(actor="p1", skill="fireball", target="dummy"),  # MP 100->70
            WaitAction(seconds=8.5),
            CastAction(actor="p1", skill="fireball", target="dummy"),  # 70->40
            WaitAction(seconds=8.5),
            CastAction(actor="p1", skill="fireball", target="dummy"),  # 40->10
            WaitAction(seconds=8.5),
            CastAction(actor="p1", skill="fireball", target="dummy"),  # 10 → 不够 30 MP
        ],
        assertions=[],
    )
    issues = static_check_case(case, _build_skill_book(), _chars())
    # 第 4 次 cast 应触发 insufficient_mp（如果先触发 cd_violation 也算缺陷）
    mp_issues = [i for i in issues if i.code == "insufficient_mp"]
    assert len(mp_issues) >= 1, f"应抓到 MP 不足；实际 issues={issues}"


def test_static_check_detects_cd_violation() -> None:
    case = TestCase(
        id="bad-cd", name="bad-cd", seed=1, sandbox="pysim:v1",
        actions=[
            CastAction(actor="p1", skill="fireball", target="dummy"),
            WaitAction(seconds=1.0),
            CastAction(actor="p1", skill="fireball", target="dummy"),  # 在 CD 内
        ],
        assertions=[],
    )
    issues = static_check_case(case, _build_skill_book(), _chars())
    cd_issues = [i for i in issues if i.code == "cd_violation"]
    assert len(cd_issues) == 1


def test_static_check_detects_interrupt_no_cast() -> None:
    case = TestCase(
        id="bad-int", name="bad-int", seed=1, sandbox="pysim:v1",
        actions=[InterruptAction(actor="p1")],
        assertions=[],
    )
    issues = static_check_case(case, _build_skill_book(), _chars())
    assert any(i.code == "interrupt_no_cast" for i in issues)


def test_static_check_clean_case_yields_no_issues() -> None:
    case = TestCase(
        id="ok", name="ok", seed=1, sandbox="pysim:v1",
        actions=[
            CastAction(actor="p1", skill="fireball", target="dummy"),
            WaitAction(seconds=1.0),
            WaitAction(seconds=8.5),
            CastAction(actor="p1", skill="frostbolt", target="dummy"),
            WaitAction(seconds=1.5),
        ],
        assertions=[],
    )
    issues = static_check_case(case, _build_skill_book(), _chars())
    assert issues == []


# --------------------------------------------------------------------------- #
# CriticAgent 完整流程（mock LLM）
# --------------------------------------------------------------------------- #


@dataclass
class _MockLLMClient:
    scripted: list[LLMResponse]
    trace: LLMTrace
    model: str = "mock"
    used_tokens: int = 0
    used_usd: float = 0.0
    _cursor: int = 0
    _calls: list[list[dict[str, Any]]] = field(default_factory=list)

    def chat(self, messages, *, tools=None, temperature=None, max_tokens=None,
             agent=None, tool_choice=None):  # noqa: ARG002
        self._calls.append(list(messages))
        if self._cursor >= len(self.scripted):
            raise AssertionError("mock 脚本耗尽")
        resp = self.scripted[self._cursor]
        self._cursor += 1
        return resp


def test_critic_agent_drops_bad_case_and_keeps_good_one(tmp_path: Path) -> None:
    """plan 含 1 条 bad（不可救）+ 1 条 good 的 case；
    Critic 应当 drop bad、accept good。
    """
    bad = TestCase(
        id="bad", name="bad", seed=1, sandbox="pysim:v1",
        actions=[InterruptAction(actor="p1")],   # 没在 cast 也 interrupt
        assertions=[],
    )
    good = TestCase(
        id="good", name="good", seed=1, sandbox="pysim:v1",
        actions=[
            CastAction(actor="p1", skill="fireball", target="dummy"),
            WaitAction(seconds=1.0),
        ],
        assertions=[Assertion(
            invariant=HpNonnegInvariant(id="I-01-d", description="", actor="dummy"),
            when=AssertionWhen.END_OF_RUN,
        )],
    )
    plan = TestPlan(id="t", cases=[bad, good])

    scripted = [
        # 1: list_cases
        LLMResponse(
            model="mock", content="",
            tool_calls=[ToolCall(id="t0", name="list_cases", arguments={})],
        ),
        # 2: drop bad + accept good（parallel）
        LLMResponse(
            model="mock", content="",
            tool_calls=[
                ToolCall(id="t1", name="drop_case",
                         arguments={"case_id": "bad", "reason": "interrupt 时无 cast"}),
                ToolCall(id="t2", name="accept_case", arguments={"case_id": "good"}),
            ],
        ),
        # 3: finalize
        LLMResponse(
            model="mock", content="",
            tool_calls=[ToolCall(id="t3", name="finalize", arguments={"reason": "done"})],
        ),
    ]
    trace = LLMTrace(path=tmp_path / "tr.jsonl", session_id="t")
    client = _MockLLMClient(scripted=scripted, trace=trace)

    result = run_critic_agent(
        plan=plan, skill_book=_build_skill_book(),
        initial_characters=_chars(),
        llm=client,  # type: ignore[arg-type]
    )
    assert result.finalized_by_agent
    assert result.dropped == 1
    assert result.accepted == 1
    assert len(result.plan.cases) == 1
    assert result.plan.cases[0].id == "good"


def test_critic_agent_patches_fixable_case(tmp_path: Path) -> None:
    """plan 含 1 条 wait 时长不够导致 cd_violation 的 case；
    Critic 应当 patch（把 wait 拉长）。
    """
    case = TestCase(
        id="cd-bad", name="cd-bad", seed=1, sandbox="pysim:v1",
        actions=[
            CastAction(actor="p1", skill="fireball", target="dummy"),
            WaitAction(seconds=1.0),
            CastAction(actor="p1", skill="fireball", target="dummy"),  # CD 内
        ],
        assertions=[],
    )
    plan = TestPlan(id="t", cases=[case])

    scripted = [
        LLMResponse(
            model="mock", content="",
            tool_calls=[ToolCall(id="t0", name="list_cases", arguments={})],
        ),
        LLMResponse(
            model="mock", content="",
            tool_calls=[ToolCall(id="t1", name="inspect_case",
                                 arguments={"case_id": "cd-bad"})],
        ),
        # patch：在两次 cast 之间多 wait 8s 让 CD 过
        LLMResponse(
            model="mock", content="",
            tool_calls=[ToolCall(
                id="t2", name="patch_case",
                arguments={
                    "case_id": "cd-bad",
                    "new_actions": [
                        {"kind": "cast", "actor": "p1", "skill": "fireball", "target": "dummy"},
                        {"kind": "wait", "seconds": 1.0},
                        {"kind": "wait", "seconds": 8.0},   # 多等 8s 让 CD 过
                        {"kind": "cast", "actor": "p1", "skill": "fireball", "target": "dummy"},
                        {"kind": "wait", "seconds": 1.0},
                    ],
                    "rationale": "在 cast 之间补 wait(8s) 让 CD 过",
                },
            )],
        ),
        LLMResponse(
            model="mock", content="",
            tool_calls=[ToolCall(id="t3", name="finalize", arguments={"reason": "done"})],
        ),
    ]
    trace = LLMTrace(path=tmp_path / "tr.jsonl", session_id="t")
    client = _MockLLMClient(scripted=scripted, trace=trace)

    result = run_critic_agent(
        plan=plan, skill_book=_build_skill_book(),
        initial_characters=_chars(), llm=client,  # type: ignore[arg-type]
    )
    assert result.patched == 1
    assert len(result.plan.cases) == 1
    new_case = result.plan.cases[0]
    assert len(new_case.actions) == 5
    # 新 actions 不再有 cd_violation
    issues = static_check_case(new_case, _build_skill_book(), _chars())
    assert all(i.code != "cd_violation" for i in issues)
