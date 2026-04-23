"""差分回归（Differential Regression）数据模型与算法。

本模块的职责

输入：两次 ``run_plan`` 产出的 ``TestSuiteResult`` —— baseline 和 candidate。
输出：一份 ``RegressDiff``，按 case_id 关联两边结果，分类为：

  - NEW     baseline PASSED, candidate FAILED/ERROR  （回归引入的新 bug）
  - FIXED   baseline FAILED/ERROR, candidate PASSED  （回归修复的旧 bug）
  - STABLE_PASS  两边都 PASSED                         （好消息，不输出）
  - STABLE_FAIL  两边都失败                            （已知 bug 未变）
  - MISSING      只在一边出现                          （plan 改了，警告）

为什么不接 Allure？

Allure 是 pytest 生态的工业标杆，但需要 Java 运行时 + `allure` CLI 来生成
HTML。GameGuard 是单模块项目，加 Java 依赖性价比低。我们用 Jinja2 + 简单
CSS 自渲染单页 HTML，覆盖 90% Allure 价值，零外部依赖。

面试可讲："评估了 Allure，发现单模块演示下 Java 依赖不抵价值，自己用
Jinja2 实现等价 90% 功能 —— 这种'按需自己造'的取舍能力，是 senior 工程师
的标志。"

和 D7 TriageAgent 的联动

`gameguard regress` 子命令会自动把 NEW failures 喂给 TriageAgent，产出
对应的 BugReport，写进 regress 报告。FIXED 不触发 triage（已不是 bug 了）。
"""
from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

from gameguard.testcase.model import CaseOutcome, TestResult, TestSuiteResult

# --------------------------------------------------------------------------- #
# RegressEntry：单条 case 在两边的结果对照
# --------------------------------------------------------------------------- #

RegressVerdict = Literal[
    "NEW",          # baseline PASS, candidate FAIL/ERROR
    "FIXED",        # baseline FAIL/ERROR, candidate PASS
    "STABLE_PASS",  # both PASS
    "STABLE_FAIL",  # both FAIL/ERROR
    "MISSING_BASELINE",   # case 仅在 candidate 出现
    "MISSING_CANDIDATE",  # case 仅在 baseline 出现
]

class RegressEntry(BaseModel):
    """单条 case 的 v1 / v2 对照。"""

    case_id: str
    case_name: str
    verdict: RegressVerdict
    baseline_outcome: str | None = None    # 'passed' / 'failed' / 'error' / None
    candidate_outcome: str | None = None
    baseline_failing_invariants: list[str] = Field(default_factory=list)
    candidate_failing_invariants: list[str] = Field(default_factory=list)
    # 跑时长便于 perf regression 检测
    baseline_wall_ms: float | None = None
    candidate_wall_ms: float | None = None

class RegressDiff(BaseModel):
    """整个 plan 跑两边的差分摘要。"""

    plan_id: str
    baseline_sandbox: str
    candidate_sandbox: str
    generated_at: datetime = Field(default_factory=datetime.now)

    # 三态计数
    new_count: int = 0
    fixed_count: int = 0
    stable_pass_count: int = 0
    stable_fail_count: int = 0
    missing_count: int = 0

    # 每条 case 的对照
    entries: list[RegressEntry] = Field(default_factory=list)

    @property
    def total(self) -> int:
        return len(self.entries)

    @property
    def has_regression(self) -> bool:
        """有 NEW failure 即视为 regression 不可发布。"""
        return self.new_count > 0

    def filter_new(self) -> list[RegressEntry]:
        return [e for e in self.entries if e.verdict == "NEW"]

    def filter_fixed(self) -> list[RegressEntry]:
        return [e for e in self.entries if e.verdict == "FIXED"]

    def summary_line(self) -> str:
        return (
            f"{self.plan_id}: NEW={self.new_count} FIXED={self.fixed_count} "
            f"STABLE_PASS={self.stable_pass_count} "
            f"STABLE_FAIL={self.stable_fail_count} MISSING={self.missing_count}"
        )

# --------------------------------------------------------------------------- #
# 核心算法：把两份 suite 拼成一份 diff
# --------------------------------------------------------------------------- #

def compute_regress_diff(
    *,
    baseline: TestSuiteResult,
    candidate: TestSuiteResult,
    plan_id: str | None = None,
) -> RegressDiff:
    """对照两份 TestSuiteResult，按 case_id 关联输出 RegressDiff。

    设计要点：
      - 按 case_id 关联（plan 改名了就会被识别为 MISSING）
      - 任何 ERROR 视为失败的等价物（"not green = bad"）
      - STABLE_PASS 仍计数但不进 entries（避免报告冗余；HTML 会单独 summary）
    """
    base_by_id = {c.case_id: c for c in baseline.cases}
    cand_by_id = {c.case_id: c for c in candidate.cases}
    all_ids = sorted(set(base_by_id) | set(cand_by_id))

    diff = RegressDiff(
        plan_id=plan_id or candidate.plan_id,
        baseline_sandbox=baseline.sandbox,
        candidate_sandbox=candidate.sandbox,
    )

    for cid in all_ids:
        b = base_by_id.get(cid)
        c = cand_by_id.get(cid)

        if b is None and c is not None:
            diff.entries.append(_entry_for(c, verdict="MISSING_BASELINE", baseline=None, candidate=c))
            diff.missing_count += 1
            continue
        if c is None and b is not None:
            diff.entries.append(_entry_for(b, verdict="MISSING_CANDIDATE", baseline=b, candidate=None))
            diff.missing_count += 1
            continue

        # 双方都有
        b_pass = _is_pass(b)
        c_pass = _is_pass(c)
        if b_pass and c_pass:
            diff.stable_pass_count += 1
            # STABLE_PASS 不进 entries（HTML 报告里聚合显示数字即可）
            continue
        if b_pass and not c_pass:
            diff.entries.append(_entry_for(c, verdict="NEW", baseline=b, candidate=c))
            diff.new_count += 1
            continue
        if not b_pass and c_pass:
            diff.entries.append(_entry_for(b, verdict="FIXED", baseline=b, candidate=c))
            diff.fixed_count += 1
            continue
        # 双方都 fail
        diff.entries.append(_entry_for(c, verdict="STABLE_FAIL", baseline=b, candidate=c))
        diff.stable_fail_count += 1

    return diff

def _is_pass(result: TestResult) -> bool:
    return result.outcome == CaseOutcome.PASSED

def _entry_for(
    representative: TestResult,
    *,
    verdict: RegressVerdict,
    baseline: TestResult | None,
    candidate: TestResult | None,
) -> RegressEntry:
    return RegressEntry(
        case_id=representative.case_id,
        case_name=representative.case_name,
        verdict=verdict,
        baseline_outcome=baseline.outcome.value if baseline else None,
        candidate_outcome=candidate.outcome.value if candidate else None,
        baseline_failing_invariants=(
            [ao.result.invariant_id for ao in baseline.failing_assertions]
            if baseline else []
        ),
        candidate_failing_invariants=(
            [ao.result.invariant_id for ao in candidate.failing_assertions]
            if candidate else []
        ),
        baseline_wall_ms=baseline.wall_time_ms if baseline else None,
        candidate_wall_ms=candidate.wall_time_ms if candidate else None,
    )
