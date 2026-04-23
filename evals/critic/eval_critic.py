"""CriticAgent 的评估脚本。

流程：
    1. 构造 fixture：一份 TestPlan 含 6 条 broken case + 4 条 correct case
    2. 跑 CriticAgent review
    3. 对比期望决策：
       - broken 应 patch 或 drop
       - correct 应 accept（或小幅 patch）
    4. 可选：对 patched case 真跑 pysim:v1，看是否真能过（patch 有效性）

跑法：
    python -m evals.critic.eval_critic --runs 1
"""
from __future__ import annotations

import argparse
import time
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

from evals.common import RunMetrics, confirm_real_run, make_llm_client
from gameguard.agents.critic import run_critic_agent
from gameguard.domain.action import CastAction, InterruptAction, WaitAction
from gameguard.domain.invariant import HpNonnegInvariant, MpNonnegInvariant
from gameguard.sandbox.pysim.factory import default_characters
from gameguard.sandbox.pysim.v1 import build_skill_book
from gameguard.testcase.model import (
    Assertion,
    AssertionWhen,
    TestCase,
    TestPlan,
    TestStrategy,
)


# --- Fixture 构造 -------------------------------------------------------------
#
# 10 条 case：6 条有问题 + 4 条正常。
# 期望 Critic 决策：broken → patch/drop；correct → accept。

def _case(id: str, actions: list, broken: bool) -> TestCase:
    return TestCase(
        id=id,
        name=id,
        sandbox="pysim:v1",
        actor="p1",
        seed=42,
        actions=actions,
        assertions=[
            Assertion(
                invariant=HpNonnegInvariant(
                    id=f"{id}-hp", description="hp never negative", actor="dummy"
                ),
                when=AssertionWhen.EVERY_TICK,
            ),
            Assertion(
                invariant=MpNonnegInvariant(
                    id=f"{id}-mp", description="mp never negative", actor="p1"
                ),
                when=AssertionWhen.EVERY_TICK,
            ),
        ],
        strategy=TestStrategy.HANDWRITTEN,
        tags=["broken"] if broken else ["correct"],
    )


def _make_fixture_plan() -> tuple[TestPlan, dict[str, bool]]:
    """返回 (plan, {case_id -> should_be_modified})。

    should_be_modified=True 表示这条 case 本身有问题，Critic 应该 patch 或 drop。
    """
    # --- broken: MP 不够施放第二个大技 ---
    broken_mp = _case(
        "broken-mp-exhaust",
        [
            CastAction(actor="p1", skill="skill_focus", target="p1"),
            WaitAction(seconds=2.5),
            CastAction(actor="p1", skill="skill_focus", target="p1"),
            WaitAction(seconds=2.5),
            CastAction(actor="p1", skill="skill_focus", target="p1"),
            WaitAction(seconds=2.5),
            CastAction(actor="p1", skill="skill_focus", target="p1"),
            WaitAction(seconds=2.5),
            CastAction(actor="p1", skill="skill_focus", target="p1"),
            WaitAction(seconds=2.5),
            CastAction(actor="p1", skill="skill_focus", target="p1"),
        ],
        broken=True,
    )

    # --- broken: skill id 拼错 ---
    broken_typo = _case(
        "broken-skill-typo",
        [
            CastAction(actor="p1", skill="skill_firball", target="dummy"),  # 拼错
            WaitAction(seconds=1.5),
        ],
        broken=True,
    )

    # --- broken: 打断空 cast ---
    broken_interrupt = _case(
        "broken-interrupt-idle",
        [
            WaitAction(seconds=0.5),
            InterruptAction(actor="p1"),  # 没在 cast，打断无效
        ],
        broken=True,
    )

    # --- broken: wait 太短，cast 没完成 ---
    broken_timing = _case(
        "broken-timing-too-short",
        [
            CastAction(actor="p1", skill="skill_focus", target="p1"),
            WaitAction(seconds=0.1),  # focus cast_time=2s，0.1s 远不够
        ],
        broken=True,
    )

    # --- broken: 另一条 MP 不够的 ---
    broken_mp2 = _case(
        "broken-mp-too-many-casts",
        [
            CastAction(actor="p1", skill="skill_fireball", target="dummy"),
            WaitAction(seconds=9.0),
            CastAction(actor="p1", skill="skill_fireball", target="dummy"),
            WaitAction(seconds=9.0),
            CastAction(actor="p1", skill="skill_fireball", target="dummy"),
            WaitAction(seconds=9.0),
            CastAction(actor="p1", skill="skill_fireball", target="dummy"),
            WaitAction(seconds=9.0),
            CastAction(actor="p1", skill="skill_fireball", target="dummy"),
        ],
        broken=True,
    )

    # --- broken: 冷却中再放 ---
    broken_cd = _case(
        "broken-cd-blocked",
        [
            CastAction(actor="p1", skill="skill_fireball", target="dummy"),
            WaitAction(seconds=1.2),
            CastAction(actor="p1", skill="skill_fireball", target="dummy"),  # 还在冷却
        ],
        broken=True,
    )

    # --- correct: 单次 fireball ---
    correct_1 = _case(
        "correct-fireball-single",
        [
            CastAction(actor="p1", skill="skill_fireball", target="dummy"),
            WaitAction(seconds=1.2),
        ],
        broken=False,
    )

    # --- correct: 两次 fireball 间隔够 ---
    correct_2 = _case(
        "correct-fireball-double-with-gap",
        [
            CastAction(actor="p1", skill="skill_fireball", target="dummy"),
            WaitAction(seconds=10.0),
            CastAction(actor="p1", skill="skill_fireball", target="dummy"),
            WaitAction(seconds=1.2),
        ],
        broken=False,
    )

    # --- correct: fireball + 打断 ---
    correct_3 = _case(
        "correct-fireball-interrupt",
        [
            CastAction(actor="p1", skill="skill_fireball", target="dummy"),
            WaitAction(seconds=0.2),
            InterruptAction(actor="p1"),
            WaitAction(seconds=0.5),
        ],
        broken=False,
    )

    # --- correct: ignite + wait ---
    correct_4 = _case(
        "correct-ignite-dot",
        [
            CastAction(actor="p1", skill="skill_ignite", target="dummy"),
            WaitAction(seconds=4.5),
        ],
        broken=False,
    )

    cases = [
        broken_mp,
        broken_typo,
        broken_interrupt,
        broken_timing,
        broken_mp2,
        broken_cd,
        correct_1,
        correct_2,
        correct_3,
        correct_4,
    ]
    plan = TestPlan(
        id="eval-critic-fixture",
        name="CriticAgent eval fixture",
        description="6 broken + 4 correct cases 用来检验 Critic 决策精度",
        cases=cases,
    )
    ground_truth = {
        "broken-mp-exhaust": True,
        "broken-skill-typo": True,
        "broken-interrupt-idle": True,
        "broken-timing-too-short": True,
        "broken-mp-too-many-casts": True,
        "broken-cd-blocked": True,
        "correct-fireball-single": False,
        "correct-fireball-double-with-gap": False,
        "correct-fireball-interrupt": False,
        "correct-ignite-dot": False,
    }
    return plan, ground_truth


# --- 评估 ---------------------------------------------------------------------

def score_critic(
    before_plan: TestPlan,
    after_plan: TestPlan,
    ground_truth: dict[str, bool],
    dropped_ids: set[str],
) -> dict[str, Any]:
    """对比 Critic 决策 vs ground truth。

    对每条 case：
      - 如果被 drop：判"Critic 认为有问题"
      - 如果 actions 被改过：判"Critic 认为有问题"（patch）
      - 否则判"Critic 接受"

    Ground truth：broken=True → 应判有问题；broken=False → 应判接受。
    """
    before_by_id = {c.id: c for c in before_plan.cases}
    after_by_id = {c.id: c for c in after_plan.cases}

    decisions: dict[str, str] = {}  # case_id → "dropped" | "patched" | "accepted"
    for cid in ground_truth:
        if cid in dropped_ids:
            decisions[cid] = "dropped"
        elif cid not in after_by_id:
            # 被移除但不在 dropped 集合里，保守当作 drop
            decisions[cid] = "dropped"
        else:
            before = before_by_id[cid]
            after = after_by_id[cid]
            if before.actions != after.actions:
                decisions[cid] = "patched"
            else:
                decisions[cid] = "accepted"

    # 期望：broken → patched/dropped；correct → accepted
    tp = fp = tn = fn = 0
    for cid, is_broken in ground_truth.items():
        dec = decisions[cid]
        flagged = dec in ("dropped", "patched")
        if is_broken and flagged:
            tp += 1
        elif is_broken and not flagged:
            fn += 1  # 漏报
        elif (not is_broken) and flagged:
            fp += 1  # 误报
        else:
            tn += 1

    precision = tp / (tp + fp) if (tp + fp) else 0.0
    recall = tp / (tp + fn) if (tp + fn) else 0.0
    accuracy = (tp + tn) / len(ground_truth)

    return {
        "precision": precision,
        "recall": recall,
        "accuracy": accuracy,
        "tp": tp,
        "fp": fp,
        "tn": tn,
        "fn": fn,
        "decisions": decisions,
    }


# --- 主流程 -------------------------------------------------------------------

def main() -> int:
    load_dotenv()
    parser = argparse.ArgumentParser()
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--out", type=str, default="evals/critic/results.md")
    args = parser.parse_args()

    plan, ground_truth = _make_fixture_plan()
    print(f"[eval] fixture: {len(plan.cases)} cases "
          f"({sum(ground_truth.values())} broken + {sum(1 for v in ground_truth.values() if not v)} correct)")

    est_usd = args.runs * 0.05
    if args.dry_run:
        print(f"[dry-run] {args.runs} 次 Critic 估计花费 ~${est_usd:.2f}")
        return 0

    confirm_real_run(est_usd, f"跑 {args.runs} 次 CriticAgent")

    runs: list[RunMetrics] = []
    details: list[dict[str, Any]] = []

    for i in range(1, args.runs + 1):
        print(f"\n[eval] Run {i}/{args.runs}...")
        client = make_llm_client(session_id=f"eval-critic-run{i}")
        # run_critic_agent 会就地改 plan，所以每次都深拷一份
        plan_copy = plan.model_copy(deep=True)
        plan_before_snapshot = plan.model_copy(deep=True)

        t0 = time.perf_counter()
        try:
            result = run_critic_agent(
                plan=plan_copy,
                skill_book=build_skill_book(),
                initial_characters=default_characters(),
                llm=client,
            )
        except Exception as e:
            print(f"  ✗ 失败: {e}")
            continue
        wall = time.perf_counter() - t0

        dropped_ids = {
            c.id for c in plan_before_snapshot.cases if c.id not in {pc.id for pc in result.plan.cases}
        }
        scored = score_critic(plan_before_snapshot, result.plan, ground_truth, dropped_ids)
        metrics = RunMetrics(
            recall=scored["recall"],
            precision=scored["precision"],
            steps=result.stats.steps,
            tokens=0,
            usd=0.0,
            wall_seconds=wall,
            extra=scored,
        )
        runs.append(metrics)
        details.append(scored)
        print(
            f"  accuracy={scored['accuracy']:.2%} "
            f"precision={scored['precision']:.2%} recall={scored['recall']:.2%} "
            f"tp={scored['tp']} fp={scored['fp']} fn={scored['fn']} "
            f"accepted={result.accepted} patched={result.patched} dropped={result.dropped} "
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
    ground_truth: dict[str, bool],
) -> str:
    lines = [
        "# CriticAgent 评估结果",
        "",
        f"- Fixture：{sum(ground_truth.values())} 条 broken + "
        f"{sum(1 for v in ground_truth.values() if not v)} 条 correct",
        f"- Runs：{len(runs)}",
        "",
        "## Fixture 明细",
        "",
        "| Case ID | 期望 |",
        "|---|---|",
    ]
    for cid, is_broken in ground_truth.items():
        lines.append(f"| `{cid}` | {'broken→修' if is_broken else 'correct→接受'} |")
    lines.append("")

    lines.extend([
        "## 各次运行",
        "",
        "| # | accuracy | precision | recall | tp | fp | fn | wall (s) |",
        "|---|---:|---:|---:|---:|---:|---:|---:|",
    ])
    for i, r in enumerate(runs, 1):
        e = r.extra
        lines.append(
            f"| {i} | {e['accuracy']:.2%} | {e['precision']:.2%} | "
            f"{e['recall']:.2%} | {e['tp']} | {e['fp']} | {e['fn']} | "
            f"{r.wall_seconds:.1f} |"
        )
    avg_acc = sum(d['accuracy'] for d in details) / len(details)
    avg_p = sum(r.precision for r in runs) / len(runs)
    avg_r = sum(r.recall for r in runs) / len(runs)
    lines.append(
        f"| **mean** | **{avg_acc:.2%}** | **{avg_p:.2%}** | **{avg_r:.2%}** | — | — | — | "
        f"{sum(r.wall_seconds for r in runs)/len(runs):.1f} |"
    )
    lines.append("")

    lines.append("## 结论")
    lines.append("")
    if avg_acc >= 0.9:
        lines.append("✓ Critic 决策质量很高（≥ 90% accuracy）")
    elif avg_acc >= 0.7:
        lines.append(f"△ Critic 能识别大部分问题（accuracy {avg_acc:.0%}）")
    else:
        lines.append(f"✗ Critic 决策质量不达标：accuracy {avg_acc:.0%}")
    lines.append("")

    return "\n".join(lines)


if __name__ == "__main__":
    raise SystemExit(main())
