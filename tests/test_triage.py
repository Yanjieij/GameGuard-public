"""D7 meta-tests —— TriageAgent 聚类 + BugReport 拼装。

验证项：
  1. 规则阶段 cluster_failures 按 invariant_id 前缀正确分组
  2. ERROR 用例按 error_message 前缀分组
  3. emit_bug_report 自动套 severity 映射表
  4. AgentLoop 用 mock LLM 跑通完整 triage 流程，产出预期 bug 数
  5. 端到端：load_suite_from_json + run_triage_from_json
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


from gameguard.agents.triage import run_triage_agent
from gameguard.domain.invariant import InvariantResult
from gameguard.llm.client import LLMResponse, ToolCall
from gameguard.llm.trace import LLMTrace
from gameguard.reports.markdown import render_bug_reports
from gameguard.reports.schema import (
    SEVERITY_BY_INVARIANT_KIND,
)
from gameguard.testcase.model import (
    AssertionOutcome,
    AssertionWhen,
    CaseOutcome,
    TestResult,
    TestSuiteResult,
)
from gameguard.tools.triage_tools import cluster_failures


# =========================================================================== #
# Helpers — 构造假的 TestSuiteResult
# =========================================================================== #


def _make_failed_case(
    case_id: str, invariant_id: str, message: str = "boom"
) -> TestResult:
    return TestResult(
        case_id=case_id,
        case_name=f"case {case_id}",
        outcome=CaseOutcome.FAILED,
        seed=1,
        sandbox="pysim:v2",
        ticks_elapsed=10,
        sim_time=0.5,
        wall_time_ms=1.0,
        assertion_results=[
            AssertionOutcome(
                assertion_invariant_id=invariant_id,
                when=AssertionWhen.END_OF_RUN,
                result=InvariantResult(
                    invariant_id=invariant_id,
                    passed=False,
                    message=message,
                    witness_t=0.5,
                    witness_tick=10,
                    actual="0.0",
                    expected="6.4",
                ),
            )
        ],
        trace_path=f"artifacts/traces/{case_id}.jsonl",
        event_count=42,
    )


def _make_error_case(case_id: str, error: str) -> TestResult:
    return TestResult(
        case_id=case_id,
        case_name=f"case {case_id}",
        outcome=CaseOutcome.ERROR,
        seed=1,
        sandbox="pysim:v2",
        ticks_elapsed=5,
        sim_time=0.25,
        wall_time_ms=1.0,
        error_message=error,
        trace_path=f"artifacts/traces/{case_id}.jsonl",
        event_count=10,
    )


def _make_passed_case(case_id: str) -> TestResult:
    return TestResult(
        case_id=case_id,
        case_name=case_id,
        outcome=CaseOutcome.PASSED,
        seed=1,
        sandbox="pysim:v2",
        ticks_elapsed=5,
        sim_time=0.25,
        wall_time_ms=1.0,
    )


def _make_suite(*results: TestResult, sandbox: str = "pysim:v2") -> TestSuiteResult:
    return TestSuiteResult(
        plan_id="test_plan",
        plan_version="0.1",
        sandbox=sandbox,
        total=len(results),
        passed=sum(1 for r in results if r.outcome == CaseOutcome.PASSED),
        failed=sum(1 for r in results if r.outcome == CaseOutcome.FAILED),
        errored=sum(1 for r in results if r.outcome == CaseOutcome.ERROR),
        wall_time_ms=10.0,
        cases=list(results),
    )


# =========================================================================== #
# 1. 规则聚类
# =========================================================================== #


def test_rule_cluster_groups_by_invariant_id_prefix() -> None:
    """两条 I-04-* 失败 + 一条 I-05-* 失败 → 应分成 2 簇。"""
    suite = _make_suite(
        _make_failed_case("c1", "I-04-fireball"),
        _make_failed_case("c2", "I-04-frostbolt"),
        _make_failed_case("c3", "I-05-chilled"),
        _make_passed_case("c4"),
    )
    clusters = cluster_failures(suite)
    assert len(clusters) == 2
    sizes = sorted(c.size for c in clusters)
    assert sizes == [1, 2]


def test_rule_cluster_groups_errors_by_message_hash() -> None:
    """两条相同 error_message 的 ERROR 应被聚到一起。"""
    suite = _make_suite(
        _make_error_case("e1", "RuntimeError: insufficient mp"),
        _make_error_case("e2", "RuntimeError: insufficient mp"),
        _make_error_case("e3", "RuntimeError: skill on cooldown"),
    )
    clusters = cluster_failures(suite)
    assert len(clusters) == 2
    sizes = sorted(c.size for c in clusters)
    assert sizes == [1, 2]


def test_severity_mapping_table_has_all_known_kinds() -> None:
    """severity 映射表覆盖 invariant.py 里所有 kind（除 replay 走特殊路径）。"""
    expected_kinds = {
        "hp_nonneg", "mp_nonneg", "cooldown_at_least_after_cast",
        "buff_refresh_magnitude_stable", "buff_stacks_within_limit",
        "interrupt_clears_casting", "interrupt_refunds_mp",
        # replay_deterministic 已在表内（D8 evaluator 后才会触发）
        "replay_deterministic",
        # dot_total_damage 在表内（D8 evaluator 后才会触发）
        "dot_total_damage_within_tolerance",
    }
    assert expected_kinds <= set(SEVERITY_BY_INVARIANT_KIND)


# =========================================================================== #
# 2. AgentLoop 用 mock LLM 跑完整 triage
# =========================================================================== #


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


def test_triage_agent_emits_one_bug_per_cluster(tmp_path: Path) -> None:
    """3 簇失败 → mock LLM 走 list_failures → emit×3 → finalize → 应有 3 条 bug。"""
    suite = _make_suite(
        _make_failed_case("c1", "I-04-fireball"),
        _make_failed_case("c2", "I-04-frostbolt"),  # 与 c1 同 cluster (failed-I-04)
        _make_failed_case("c3", "I-05-chilled"),
        _make_error_case("e1", "RuntimeError: insufficient mp"),
    )
    # 期望 3 个 cluster：failed-I-04（含 c1+c2）、failed-I-05（含 c3）、error-xxx（含 e1）
    pre_clusters = cluster_failures(suite)
    assert len(pre_clusters) == 3
    cluster_ids = sorted(c.cluster_id for c in pre_clusters)

    # mock LLM 脚本：1 轮 list_failures → 1 轮 emit×3 + finalize（parallel）
    scripted = [
        LLMResponse(
            model="mock", content="Let me see the failures.",
            tool_calls=[ToolCall(id="t0", name="list_failures", arguments={})],
        ),
        LLMResponse(
            model="mock", content="Triage now.",
            tool_calls=[
                ToolCall(
                    id=f"e{i}",
                    name="emit_bug_report",
                    arguments={
                        "cluster_id": cid,
                        "title": f"bug for {cid}",
                        "component": "Skill.Cooldown",
                        "repro_steps": ["重置沙箱", "释放技能", "检查冷却"],
                        "expected": "正常",
                        "actual": "异常",
                        "tags": ["regression"],
                        "rationale": "规则聚类",
                    },
                )
                for i, cid in enumerate(cluster_ids)
            ] + [
                ToolCall(id="fin", name="finalize", arguments={"reason": "all done"}),
            ],
        ),
    ]
    trace = LLMTrace(path=tmp_path / "tr.jsonl", session_id="t")
    client = _MockLLMClient(scripted=scripted, trace=trace)

    result = run_triage_agent(suite=suite, llm=client)  # type: ignore[arg-type]
    assert result.finalized_by_agent
    assert result.output.total_failures == 4
    assert result.output.total_bugs == 3
    # 每条 bug 应有合理 severity（默认按 cooldown -> S2，error -> S2）
    severities = [b.severity for b in result.output.bugs]
    assert all(s in ("S0", "S1", "S2", "S3") for s in severities)
    # cluster_size 应正确 reflect 规则阶段聚类（c1+c2 = 2，其它 1）
    sizes = sorted(b.cluster_size for b in result.output.bugs)
    assert sizes == [1, 1, 2]


def test_triage_agent_skips_when_no_failures(tmp_path: Path) -> None:
    """全过的 suite 不应调 LLM。"""
    suite = _make_suite(_make_passed_case("c1"), _make_passed_case("c2"))
    # mock LLM 故意空脚本——若被调用会 AssertionError
    trace = LLMTrace(path=tmp_path / "tr.jsonl", session_id="t")
    client = _MockLLMClient(scripted=[], trace=trace)
    result = run_triage_agent(suite=suite, llm=client)  # type: ignore[arg-type]
    assert result.output.total_bugs == 0
    assert result.output.total_failures == 0
    assert result.stats.steps == 0


# =========================================================================== #
# 3. Bug 报告渲染
# =========================================================================== #


def test_bug_report_markdown_renders(tmp_path: Path) -> None:
    suite = _make_suite(_make_failed_case("c1", "I-04-fireball"))
    pre_clusters = cluster_failures(suite)
    cluster_ids = [c.cluster_id for c in pre_clusters]

    scripted = [
        LLMResponse(
            model="mock", content="",
            tool_calls=[ToolCall(id="t0", name="list_failures", arguments={})],
        ),
        LLMResponse(
            model="mock", content="",
            tool_calls=[
                ToolCall(
                    id="e0",
                    name="emit_bug_report",
                    arguments={
                        "cluster_id": cluster_ids[0],
                        "title": "切换技能时 Fireball 冷却被错误重置",
                        "component": "Skill.Cooldown",
                        "severity": "S1",
                        "repro_steps": [
                            "用 seed=1 重置 pysim:v2",
                            "p1 释放 skill_fireball at t=0",
                            "等待 1.0s",
                            "在 t=2.0 检查 fireball cooldown",
                        ],
                        "expected": "Fireball 冷却剩余约 6.4s",
                        "actual": "Fireball 冷却被清零",
                        "suggested_owner": "skill_system_team",
                        "tags": ["regression", "BUG-001"],
                        "rationale": "切技能时 cooldowns.clear() 误清",
                    },
                ),
                ToolCall(id="fin", name="finalize", arguments={"reason": "done"}),
            ],
        ),
    ]
    trace = LLMTrace(path=tmp_path / "tr.jsonl", session_id="t")
    client = _MockLLMClient(scripted=scripted, trace=trace)
    result = run_triage_agent(suite=suite, llm=client)  # type: ignore[arg-type]
    md = render_bug_reports(result.output)

    assert "切换技能时 Fireball 冷却被错误重置" in md
    assert "S1" in md
    assert "skill_fireball" in md
    assert "BUG-001" in md
    # 严重级 badge
    assert "Critical" in md or "S1" in md
