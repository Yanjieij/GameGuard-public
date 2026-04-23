"""TestGenAgent 的评估脚本。

流程：
    1) 先跑 DesignDocAgent 产出 bundle（命中 cache 免费）
    2) 跑 TestGenAgent 生成 plan
    3) 把 plan 在 pysim:v1 上跑 → 统计 v1_pass_rate
    4) 把 plan 在 pysim:v2 上跑 → 统计抓到哪些 BUG 编号
    5) 对比 handwritten.yaml baseline
    6) 输出 evals/test_gen/results.md

跑法：
    python -m evals.test_gen.eval_test_gen --runs 1
    python -m evals.test_gen.eval_test_gen --dry-run
"""
from __future__ import annotations

import argparse
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

from evals.common import RunMetrics, confirm_real_run, make_llm_client
from gameguard.agents.design_doc import run_design_doc_agent
from gameguard.agents.test_gen import run_test_gen_agent
from gameguard.cli import resolve_sandbox_factory
from gameguard.sandbox.pysim.factory import default_characters
from gameguard.sandbox.pysim.v1 import build_skill_book
from gameguard.testcase.loader import load_plan_from_yaml
from gameguard.testcase.model import TestPlan, TestSuiteResult
from gameguard.testcase.runner import run_plan


# --- Bug ID ↔ Invariant kind 映射 --------------------------------------------
#
# v2 里每个植入 bug 对应一条或几条检测 invariant。只要对应 kind 在 v2 上有
# 任何 case 失败，就算"抓到了"。这个映射来自 v2/skills.py 的 docstring。

BUG_TO_INVARIANT_KINDS: dict[str, set[str]] = {
    "BUG-001": {"cooldown_at_least_after_cast"},
    "BUG-002": {"buff_refresh_magnitude_stable"},
    "BUG-003": {"interrupt_refunds_mp", "mp_nonneg"},
    "BUG-004": {"dot_total_damage_within_tolerance"},
    "BUG-005": {"replay_deterministic"},
}
ALL_BUG_IDS: set[str] = set(BUG_TO_INVARIANT_KINDS.keys())


# --- 评估单条 plan 的效果 -----------------------------------------------------

@dataclass
class PlanRunStats:
    """一份 plan 在某个 sandbox 上跑完的汇总。"""

    sandbox: str
    n_cases: int
    n_passed: int
    n_failed: int
    n_errored: int
    failing_invariant_kinds: set[str] = field(default_factory=set)
    caught_bugs: set[str] = field(default_factory=set)


def _override_sandbox(plan: TestPlan, sandbox: str) -> TestPlan:
    """复制 plan 并把每个 case 的 sandbox 改成给定值。"""
    new_plan = plan.model_copy(deep=True)
    for c in new_plan.cases:
        c.sandbox = sandbox
    return new_plan


def _summarize_run(
    suite: TestSuiteResult, sandbox: str, plan: TestPlan
) -> PlanRunStats:
    # 先建 invariant_id → kind 的索引（outcome 里没存 kind，只有 id）
    id_to_kind: dict[str, str] = {}
    for case in plan.cases:
        for a in case.assertions:
            id_to_kind[a.invariant.id] = a.invariant.kind

    failing_kinds: set[str] = set()
    for case in suite.cases:
        if case.outcome.value not in ("failed", "error"):
            continue
        for ao in case.assertion_results:
            if not ao.result.passed:
                kind = id_to_kind.get(ao.result.invariant_id) or id_to_kind.get(
                    ao.assertion_invariant_id
                )
                if kind:
                    failing_kinds.add(kind)

    caught = {
        bug_id
        for bug_id, kinds in BUG_TO_INVARIANT_KINDS.items()
        if kinds & failing_kinds
    }
    return PlanRunStats(
        sandbox=sandbox,
        n_cases=suite.total,
        n_passed=suite.passed,
        n_failed=suite.failed,
        n_errored=suite.errored,
        failing_invariant_kinds=failing_kinds,
        caught_bugs=caught,
    )


def evaluate_plan(plan: TestPlan, artifacts_sub: str) -> dict[str, PlanRunStats]:
    """给一份 plan，在 v1 和 v2 各跑一次，返回两组统计。"""
    out: dict[str, PlanRunStats] = {}
    for sandbox in ("pysim:v1", "pysim:v2"):
        overridden = _override_sandbox(plan, sandbox)
        artifacts = Path("artifacts/evals") / artifacts_sub / sandbox.replace(":", "_")
        suite = run_plan(overridden, resolve_sandbox_factory, artifacts_dir=artifacts)
        out[sandbox] = _summarize_run(suite, sandbox, overridden)
    return out


# --- 主流程 -------------------------------------------------------------------

def run_one(run_index: int, prefetch: bool) -> tuple[TestPlan, float]:
    """跑 DesignDoc → TestGen 管线，返回产出的 plan 和 wall-clock 秒。"""
    session_id = f"eval-test-gen-run{run_index}{'-prefetch' if prefetch else ''}"
    client = make_llm_client(session_id=session_id)

    t0 = time.perf_counter()
    # 复用 DesignDoc 的 cache（Stage 1.2 已经调过）
    dd_result = run_design_doc_agent(
        doc_paths=[Path("docs/example_skill_v1.md")],
        llm=client,
    )
    tg_result = run_test_gen_agent(
        bundle=dd_result.bundle,
        skill_book=build_skill_book(),
        initial_characters=default_characters(),
        llm=client,
        plan_id=session_id,
        plan_name=f"Eval plan ({session_id})",
        prefetch_context=prefetch,
    )
    wall = time.perf_counter() - t0
    return tg_result.plan, wall


def main() -> int:
    load_dotenv()
    parser = argparse.ArgumentParser()
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument("--prefetch", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--out", type=str, default="evals/test_gen/results.md")
    args = parser.parse_args()

    # 1. baseline：跑 handwritten.yaml
    print("[eval] 先跑 handwritten baseline...")
    handwritten = load_plan_from_yaml("testcases/skill_system/handwritten.yaml")
    baseline_stats = evaluate_plan(handwritten, "handwritten")
    print(
        f"  handwritten v1: {baseline_stats['pysim:v1'].n_passed}/{baseline_stats['pysim:v1'].n_cases} pass"
    )
    print(
        f"  handwritten v2: 抓到 bugs = {sorted(baseline_stats['pysim:v2'].caught_bugs)}"
    )

    # 2. Agent 生成版本
    est_usd = args.runs * 0.10
    if args.dry_run:
        print(f"\n[dry-run] {args.runs} 次 Agent 生成估计花费 ~${est_usd:.2f}")
        return 0

    confirm_real_run(est_usd, f"跑 {args.runs} 次 TestGenAgent（+ 上游 DesignDoc）")

    runs: list[RunMetrics] = []
    agent_details: list[dict[str, Any]] = []

    for i in range(1, args.runs + 1):
        print(f"\n[eval] Run {i}/{args.runs}...")
        try:
            plan, wall = run_one(i, args.prefetch)
        except Exception as e:
            print(f"  ✗ 生成失败: {e}")
            continue

        stats = evaluate_plan(plan, f"agent-run{i}")
        v1 = stats["pysim:v1"]
        v2 = stats["pysim:v2"]
        v1_pass_rate = v1.n_passed / v1.n_cases if v1.n_cases > 0 else 0.0
        v2_bug_recall = len(v2.caught_bugs) / len(ALL_BUG_IDS)

        metrics = RunMetrics(
            recall=v2_bug_recall,
            precision=v1_pass_rate,
            steps=0,
            tokens=0,
            usd=0.0,
            wall_seconds=wall,
            extra={
                "n_cases": v1.n_cases,
                "v1_passed": v1.n_passed,
                "v1_failed": v1.n_failed,
                "v1_errored": v1.n_errored,
                "v2_caught_bugs": sorted(v2.caught_bugs),
                "v2_missed_bugs": sorted(ALL_BUG_IDS - v2.caught_bugs),
            },
        )
        runs.append(metrics)
        agent_details.append({"stats": stats, "metrics": metrics})
        print(
            f"  生成 {v1.n_cases} 条；v1 pass={v1.n_passed}/{v1.n_cases}; "
            f"v2 抓到 {sorted(v2.caught_bugs)}；wall={wall:.1f}s"
        )

    if not runs:
        print("所有 run 都失败。")
        return 2

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        _render_results(runs, agent_details, baseline_stats, args.prefetch),
        encoding="utf-8",
    )
    print(f"\n[eval] 结果已写入 {out_path}")
    return 0


def _render_results(
    runs: list[RunMetrics],
    details: list[dict[str, Any]],
    baseline: dict[str, PlanRunStats],
    prefetch: bool,
) -> str:
    """渲染 results.md。"""
    bl_v1 = baseline["pysim:v1"]
    bl_v2 = baseline["pysim:v2"]
    bl_pass = bl_v1.n_passed / bl_v1.n_cases if bl_v1.n_cases else 0
    bl_recall = len(bl_v2.caught_bugs) / len(ALL_BUG_IDS)

    lines = [
        "# TestGenAgent 评估结果",
        "",
        f"- 模式：{'prefetch' if prefetch else 'discovery'}",
        f"- Runs：{len(runs)}",
        "",
        "## Baseline（handwritten.yaml）",
        "",
        f"- 用例数：{bl_v1.n_cases}",
        f"- v1 pass 率：{bl_pass:.0%} ({bl_v1.n_passed}/{bl_v1.n_cases})",
        f"- v2 抓到的 bugs：{sorted(bl_v2.caught_bugs) or '无'}",
        f"- v2 bug 召回：{bl_recall:.0%} ({len(bl_v2.caught_bugs)}/{len(ALL_BUG_IDS)})",
        "",
        "## Agent 生成 vs Baseline",
        "",
        "| # | 用例数 | v1 pass% | v2 抓到 bugs | v2 召回 | wall (s) |",
        "|---|---:|---:|---|---:|---:|",
        f"| baseline (handwritten) | {bl_v1.n_cases} | {bl_pass:.0%} | "
        f"{sorted(bl_v2.caught_bugs) or '—'} | {bl_recall:.0%} | — |",
    ]

    total_cases = 0
    for i, (run, det) in enumerate(zip(runs, details), 1):
        extra = run.extra
        lines.append(
            f"| Agent run {i} | {extra['n_cases']} | "
            f"{run.precision:.0%} ({extra['v1_passed']}/{extra['n_cases']}) | "
            f"{extra['v2_caught_bugs'] or '—'} | {run.recall:.0%} | "
            f"{run.wall_seconds:.1f} |"
        )
        total_cases += extra["n_cases"]

    # 平均
    avg_pass = sum(r.precision for r in runs) / len(runs)
    avg_recall = sum(r.recall for r in runs) / len(runs)
    lines.append(
        f"| **Agent mean** | {total_cases / len(runs):.1f} | "
        f"**{avg_pass:.0%}** | — | **{avg_recall:.0%}** | "
        f"{sum(r.wall_seconds for r in runs) / len(runs):.1f} |"
    )
    lines.append("")

    # 每个 bug 的覆盖情况
    lines.append("## 每个 BUG 被 Agent 抓到的次数")
    lines.append("")
    lines.append("| Bug | Agent 抓到次数 | baseline |")
    lines.append("|---|---:|---|")
    for bug in sorted(ALL_BUG_IDS):
        agent_hits = sum(
            1 for r in runs if bug in r.extra["v2_caught_bugs"]
        )
        bl_hit = "✓" if bug in bl_v2.caught_bugs else "✗"
        lines.append(f"| {bug} | {agent_hits}/{len(runs)} | {bl_hit} |")
    lines.append("")

    # 结论
    lines.append("## 结论")
    lines.append("")
    if avg_recall >= bl_recall and avg_pass >= 0.7:
        verdict = (
            f"✓ Agent 达到 baseline 水平：v2 召回 {avg_recall:.0%} "
            f"(baseline {bl_recall:.0%})，v1 pass {avg_pass:.0%}"
        )
    elif avg_recall >= 0.6:
        verdict = (
            f"△ Agent 接近 baseline：v2 召回 {avg_recall:.0%} vs baseline {bl_recall:.0%}"
            "；差距主要在漏抓某些 bug 类型（可能因为 invariant bundle 上游漏抽）"
        )
    else:
        verdict = f"✗ Agent 明显不及 baseline：召回 {avg_recall:.0%}"
    lines.append(verdict)
    lines.append("")

    return "\n".join(lines)


if __name__ == "__main__":
    raise SystemExit(main())
