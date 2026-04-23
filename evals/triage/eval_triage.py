"""TriageAgent 的评估脚本。

流程：
    1. 跑 handwritten.yaml 在 pysim:v2 上，得到 TestSuiteResult（真实 5-bug 失败）
    2. 根据 BUG-to-kind 映射，把失败 case 按"预期同根"分组 = ground truth
    3. 跑 TriageAgent 对这份 suite 做聚类
    4. 对比：
       - cluster_recall  : 同根的 case 被合到一起的比例
       - cluster_precision: 不同根的 case 没被错合的比例
       - title_quality   : 生成的 bug 标题是否包含关键字（定性，打 0/1）

跑法：
    python -m evals.triage.eval_triage --runs 1
"""
from __future__ import annotations

import argparse
import time
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

from evals.common import RunMetrics, confirm_real_run, make_llm_client
from evals.test_gen.eval_test_gen import (
    BUG_TO_INVARIANT_KINDS,
    _override_sandbox,
)
from gameguard.agents.triage import run_triage_agent
from gameguard.cli import resolve_sandbox_factory
from gameguard.testcase.loader import load_plan_from_yaml
from gameguard.testcase.model import TestPlan, TestSuiteResult
from gameguard.testcase.runner import run_plan


# --- Ground truth 构造 --------------------------------------------------------

def _case_to_bug_ids(
    case_id: str, plan: TestPlan, suite: TestSuiteResult
) -> set[str]:
    """一条 case 的失败映射到哪些 BUG-00X。

    逻辑：看这条 case 失败的 invariant kind 集合，和每个 BUG 的 kind 集合求交。
    """
    # invariant_id → kind
    id_to_kind: dict[str, str] = {}
    for c in plan.cases:
        if c.id == case_id:
            for a in c.assertions:
                id_to_kind[a.invariant.id] = a.invariant.kind

    failing_kinds: set[str] = set()
    for result in suite.cases:
        if result.case_id != case_id:
            continue
        for ao in result.assertion_results:
            if not ao.result.passed:
                kind = id_to_kind.get(ao.result.invariant_id) or id_to_kind.get(
                    ao.assertion_invariant_id
                )
                if kind:
                    failing_kinds.add(kind)

    return {
        bug_id
        for bug_id, kinds in BUG_TO_INVARIANT_KINDS.items()
        if kinds & failing_kinds
    }


def _build_ground_truth(plan: TestPlan, suite: TestSuiteResult) -> dict[str, set[str]]:
    """产出 BUG_ID → { 应被聚在一起的 case_id 集合 }。"""
    gt: dict[str, set[str]] = {b: set() for b in BUG_TO_INVARIANT_KINDS}
    for result in suite.cases:
        if result.outcome.value not in ("failed", "error"):
            continue
        bugs = _case_to_bug_ids(result.case_id, plan, suite)
        for b in bugs:
            gt[b].add(result.case_id)
    # 移除没有 case 的 bug 组（v2 没触发的）
    return {b: cs for b, cs in gt.items() if cs}


# --- Agent 聚类 → 分组 --------------------------------------------------------

def _agent_clusters(agent_output: Any) -> list[set[str]]:
    """从 TriageOutput 提取 { case_id 集合 } 列表，每个 BugReport 一组。"""
    clusters: list[set[str]] = []
    for bug in agent_output.bugs:
        member_ids: set[str] = set()
        for member in getattr(bug, "member_case_ids", []) or []:
            member_ids.add(member)
        # 如果没有 member_case_ids，尝试从 repro_steps 里碰一下（保守，可能拿不到）
        if not member_ids and hasattr(bug, "repro_case_id"):
            cid = getattr(bug, "repro_case_id", None)
            if cid:
                member_ids.add(cid)
        if member_ids:
            clusters.append(member_ids)
    return clusters


# --- 打分 ---------------------------------------------------------------------

def score(
    ground_truth: dict[str, set[str]], agent_clusters: list[set[str]]
) -> dict[str, Any]:
    """对比 agent clusters 和 ground truth。

    cluster_recall：对每组同根，最多有多大比例的 case 被 agent 聚进同一个 cluster
    cluster_precision：agent 每个 cluster 里，同组 case 占比的最大值（有没有错合）
    """
    gt_groups = list(ground_truth.values())

    # recall per ground-truth group
    recalls: list[float] = []
    for gt_group in gt_groups:
        # 找哪个 agent cluster 和这组重叠最多
        best_overlap = max(
            (len(gt_group & ac) for ac in agent_clusters), default=0
        )
        recalls.append(best_overlap / len(gt_group) if gt_group else 0.0)
    cluster_recall = sum(recalls) / len(recalls) if recalls else 0.0

    # precision per agent cluster：cluster 里最大同组的占比
    precs: list[float] = []
    for ac in agent_clusters:
        best_match = max(
            (len(ac & gt_group) for gt_group in gt_groups), default=0
        )
        precs.append(best_match / len(ac) if ac else 0.0)
    cluster_precision = sum(precs) / len(precs) if precs else 0.0

    return {
        "cluster_recall": cluster_recall,
        "cluster_precision": cluster_precision,
        "n_gt_groups": len(gt_groups),
        "n_agent_clusters": len(agent_clusters),
        "gt_groups": [sorted(g) for g in gt_groups],
        "agent_clusters": [sorted(c) for c in agent_clusters],
    }


# --- 主流程 -------------------------------------------------------------------

def _prepare_fixture() -> tuple[TestPlan, TestSuiteResult]:
    """跑 handwritten.yaml 在 pysim:v2 上，返回 (plan, suite)。"""
    plan = load_plan_from_yaml("testcases/skill_system/handwritten.yaml")
    plan = _override_sandbox(plan, "pysim:v2")
    suite = run_plan(
        plan,
        resolve_sandbox_factory,
        artifacts_dir=Path("artifacts/evals/triage_fixture"),
    )
    return plan, suite


def main() -> int:
    load_dotenv()
    parser = argparse.ArgumentParser()
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--out", type=str, default="evals/triage/results.md")
    args = parser.parse_args()

    print("[eval] 准备 fixture：跑 handwritten.yaml 在 pysim:v2 上...")
    plan, suite = _prepare_fixture()
    print(
        f"  suite: {suite.total} cases, "
        f"passed={suite.passed}, failed={suite.failed}, errored={suite.errored}"
    )

    if suite.failed + suite.errored == 0:
        print("  ⚠ fixture 没有失败 case——TriageAgent 没东西可聚类")
        return 1

    ground_truth = _build_ground_truth(plan, suite)
    print(f"  ground truth: {len(ground_truth)} 个预期 bug 组")
    for bug, cases in ground_truth.items():
        print(f"    {bug}: {sorted(cases)}")

    est_usd = args.runs * 0.05
    if args.dry_run:
        print(f"\n[dry-run] {args.runs} 次 Triage 估计花费 ~${est_usd:.2f}")
        return 0

    confirm_real_run(est_usd, f"跑 {args.runs} 次 TriageAgent")

    runs: list[RunMetrics] = []
    details: list[dict[str, Any]] = []

    for i in range(1, args.runs + 1):
        print(f"\n[eval] Run {i}/{args.runs}...")
        client = make_llm_client(session_id=f"eval-triage-run{i}")
        t0 = time.perf_counter()
        try:
            result = run_triage_agent(suite=suite, llm=client)
        except Exception as e:
            print(f"  ✗ 失败: {e}")
            continue
        wall = time.perf_counter() - t0

        agent_clusters = _agent_clusters(result.output)
        scored = score(ground_truth, agent_clusters)
        metrics = RunMetrics(
            recall=scored["cluster_recall"],
            precision=scored["cluster_precision"],
            steps=result.stats.steps,
            tokens=0,
            usd=0.0,
            wall_seconds=wall,
            extra=scored,
        )
        runs.append(metrics)
        details.append(scored)
        print(
            f"  cluster_recall={scored['cluster_recall']:.2%} "
            f"cluster_precision={scored['cluster_precision']:.2%} "
            f"gt_groups={scored['n_gt_groups']} agent_clusters={scored['n_agent_clusters']} "
            f"wall={wall:.1f}s"
        )

    if not runs:
        return 2

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(_render_results(runs, details, ground_truth), encoding="utf-8")
    print(f"\n[eval] 结果已写入 {out_path}")
    return 0


def _render_results(
    runs: list[RunMetrics],
    details: list[dict[str, Any]],
    ground_truth: dict[str, set[str]],
) -> str:
    lines = [
        "# TriageAgent 评估结果",
        "",
        "- Fixture：handwritten.yaml 在 pysim:v2 上跑（真实 5-bug 失败）",
        f"- Ground truth bug 组：{len(ground_truth)}",
        "",
        "## Ground Truth",
        "",
    ]
    for bug, cases in sorted(ground_truth.items()):
        lines.append(f"- **{bug}**：`{sorted(cases)}`")
    lines.append("")

    lines.extend([
        "## 各次运行",
        "",
        "| # | cluster_recall | cluster_precision | agent clusters | wall (s) |",
        "|---|---:|---:|---:|---:|",
    ])
    for i, r in enumerate(runs, 1):
        lines.append(
            f"| {i} | {r.recall:.2%} | {r.precision:.2%} | "
            f"{r.extra['n_agent_clusters']} | {r.wall_seconds:.1f} |"
        )
    avg_r = sum(r.recall for r in runs) / len(runs)
    avg_p = sum(r.precision for r in runs) / len(runs)
    lines.append(
        f"| **mean** | **{avg_r:.2%}** | **{avg_p:.2%}** | — | "
        f"{sum(r.wall_seconds for r in runs)/len(runs):.1f} |"
    )
    lines.append("")

    lines.append("## 结论")
    lines.append("")
    if avg_r >= 0.8 and avg_p >= 0.8:
        verdict = "✓ Triage 聚类质量可用"
    elif avg_r >= 0.6:
        verdict = f"△ 聚类尚可（召回 {avg_r:.0%}），但仍有漏合或错合"
    else:
        verdict = f"✗ 聚类质量不达标：召回 {avg_r:.0%}、准确 {avg_p:.0%}"
    lines.append(verdict)
    lines.append("")

    return "\n".join(lines)


if __name__ == "__main__":
    raise SystemExit(main())
