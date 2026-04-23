"""D9 meta-tests —— RegressDiff 计算 + HTML 渲染。"""
from __future__ import annotations

from pathlib import Path

from gameguard.domain.invariant import InvariantResult
from gameguard.reports.html import render_regress_html
from gameguard.reports.regress import compute_regress_diff
from gameguard.reports.schema import BugReport, TriageOutput
from gameguard.testcase.model import (
    AssertionOutcome,
    AssertionWhen,
    CaseOutcome,
    TestResult,
    TestSuiteResult,
)


def _passed(case_id: str) -> TestResult:
    return TestResult(
        case_id=case_id, case_name=case_id, outcome=CaseOutcome.PASSED,
        seed=1, sandbox="x", ticks_elapsed=10, sim_time=0.5, wall_time_ms=1.0,
    )


def _failed(case_id: str, inv_id: str = "I-04") -> TestResult:
    return TestResult(
        case_id=case_id, case_name=case_id, outcome=CaseOutcome.FAILED,
        seed=1, sandbox="x", ticks_elapsed=10, sim_time=0.5, wall_time_ms=1.0,
        assertion_results=[AssertionOutcome(
            assertion_invariant_id=inv_id,
            when=AssertionWhen.END_OF_RUN,
            result=InvariantResult(invariant_id=inv_id, passed=False, message="boom"),
        )],
    )


def _suite(*results, sandbox: str = "pysim:v1", plan_id: str = "p") -> TestSuiteResult:
    return TestSuiteResult(
        plan_id=plan_id, plan_version="0.1", sandbox=sandbox,
        total=len(results),
        passed=sum(1 for r in results if r.outcome == CaseOutcome.PASSED),
        failed=sum(1 for r in results if r.outcome == CaseOutcome.FAILED),
        errored=sum(1 for r in results if r.outcome == CaseOutcome.ERROR),
        wall_time_ms=10.0, cases=list(results),
    )


# --------------------------------------------------------------------------- #
# RegressDiff 分类正确性
# --------------------------------------------------------------------------- #


def test_regress_diff_classifies_new_fixed_stable_correctly() -> None:
    baseline = _suite(_passed("c1"), _passed("c2"), _failed("c3"), sandbox="pysim:v1")
    candidate = _suite(_passed("c1"), _failed("c2"), _passed("c3"), sandbox="pysim:v2")
    diff = compute_regress_diff(baseline=baseline, candidate=candidate)

    assert diff.new_count == 1            # c2: pass -> fail
    assert diff.fixed_count == 1          # c3: fail -> pass
    assert diff.stable_pass_count == 1    # c1: pass / pass
    assert diff.stable_fail_count == 0
    assert diff.missing_count == 0
    # has_regression 仅看 NEW
    assert diff.has_regression is True
    # entries 不应包含 STABLE_PASS
    verdicts = sorted(e.verdict for e in diff.entries)
    assert verdicts == ["FIXED", "NEW"]


def test_regress_diff_no_regression_when_only_fixed() -> None:
    baseline = _suite(_failed("c1"))
    candidate = _suite(_passed("c1"))
    diff = compute_regress_diff(baseline=baseline, candidate=candidate)
    assert diff.has_regression is False
    assert diff.new_count == 0
    assert diff.fixed_count == 1


def test_regress_diff_missing_cases_flagged() -> None:
    baseline = _suite(_passed("c1"), _passed("c2"))
    candidate = _suite(_passed("c1"), _passed("c3"))
    diff = compute_regress_diff(baseline=baseline, candidate=candidate)
    assert diff.missing_count == 2
    verdicts = sorted(e.verdict for e in diff.entries)
    assert verdicts == ["MISSING_BASELINE", "MISSING_CANDIDATE"]


def test_regress_diff_summary_line_stable() -> None:
    baseline = _suite(_passed("c1"), _failed("c2"))
    candidate = _suite(_passed("c1"), _failed("c2"))
    diff = compute_regress_diff(baseline=baseline, candidate=candidate, plan_id="myplan")
    line = diff.summary_line()
    assert "myplan" in line and "NEW=0" in line and "STABLE_FAIL=1" in line


# --------------------------------------------------------------------------- #
# HTML 渲染
# --------------------------------------------------------------------------- #


def test_regress_html_renders_with_new_fixed_and_triage(tmp_path: Path) -> None:
    """完整 HTML 渲染含 NEW + FIXED + 嵌入 BugReport。"""
    baseline = _suite(_passed("c1"), _passed("c2"), _failed("old-bug"), sandbox="pysim:v1")
    candidate = _suite(_failed("c1", "I-04-fireball"), _passed("c2"), _passed("old-bug"),
                       sandbox="pysim:v2")
    diff = compute_regress_diff(baseline=baseline, candidate=candidate)

    triage = TriageOutput(
        plan_id="p", sandbox="pysim:v2",
        total_failures=1, total_bugs=1,
        bugs=[BugReport(
            bug_id="GG-test-001",
            title="切换技能时火球术冷却被错误重置",
            severity="S2",
            component="Skill.Cooldown",
            version_introduced="pysim:v2",
            repro_steps=["重置沙箱", "释放火球术", "检查冷却"],
            expected="6.4s 冷却",
            actual="0s 冷却",
            invariant_violated="I-04-fireball",
            cluster_size=1,
            representative_case_id="c1",
            cluster_rationale="单条孤例",
        )],
    )

    html = render_regress_html(diff, triage=triage)
    assert "差分回归报告" in html
    assert "NEW（回归）" in html
    assert "1" in html  # NEW count
    assert "切换技能时火球术冷却" in html
    assert "S2" in html
    assert "I-04-fireball" in html
    assert "FIXED" in html  # 修复区块
    assert "old-bug" in html
    # 颜色样式
    assert "verdict-NEW" in html


def test_regress_html_no_triage_when_clean(tmp_path: Path) -> None:
    baseline = _suite(_passed("c1"))
    candidate = _suite(_passed("c1"))
    diff = compute_regress_diff(baseline=baseline, candidate=candidate)
    html = render_regress_html(diff, triage=None)
    assert diff.has_regression is False
    # 没 NEW 段；也没 triage 段
    assert "🚨 回归用例" not in html
    assert "自动 Triage" not in html
