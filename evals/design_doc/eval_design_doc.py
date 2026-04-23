"""DesignDocAgent 的评估脚本。

跑法：
    python -m evals.design_doc.eval_design_doc           # 默认 5 次
    python -m evals.design_doc.eval_design_doc --runs 1  # 快速验证 1 次
    python -m evals.design_doc.eval_design_doc --runs 3 --dry-run   # 只看成本估算

输出：
    evals/design_doc/results.md   本次跑的数字
"""
from __future__ import annotations

import argparse
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv

from evals.common import (
    RunMetrics,
    confirm_real_run,
    make_llm_client,
    render_metrics_table,
)
from gameguard.agents.design_doc import DesignDocResult, run_design_doc_agent
from gameguard.domain.invariant import InvariantBundle


# --- 匹配逻辑 -----------------------------------------------------------------

@dataclass
class InvariantKey:
    """把 Invariant 压扁成一个可哈希的 key，用来和 golden 对比。

    注意：我们只比对关键定位字段（kind / actor / skill / buff），忽略 id、
    description、tolerance 这类不影响语义的字段。不同 Agent 抽出来的 id
    字符串几乎肯定不同。
    """

    kind: str
    actor: str | None = None
    skill: str | None = None
    buff: str | None = None

    def __hash__(self) -> int:
        return hash((self.kind, self.actor, self.skill, self.buff))

    def __str__(self) -> str:
        parts = [self.kind]
        for k, v in [("actor", self.actor), ("skill", self.skill), ("buff", self.buff)]:
            if v:
                parts.append(f"{k}={v}")
        return "  ".join(parts)


def key_from_dict(d: dict) -> InvariantKey:
    """golden yaml 条目 → InvariantKey。"""
    return InvariantKey(
        kind=d["kind"],
        actor=d.get("actor"),
        skill=d.get("skill"),
        buff=d.get("buff"),
    )


def key_from_invariant(inv: Any) -> InvariantKey:
    """Agent 产出的 Invariant 对象 → InvariantKey。

    每个 Invariant 子类的字段不完全一样，我们只抽出必要的 4 个。
    """
    return InvariantKey(
        kind=inv.kind,
        actor=getattr(inv, "actor", None),
        skill=getattr(inv, "skill", None),
        buff=getattr(inv, "buff", None),
    )


# --- 加载 golden + 打分 -------------------------------------------------------

def load_golden(path: Path) -> tuple[set[InvariantKey], set[InvariantKey]]:
    """读 golden yaml，返回 (required, optional) 两个集合。"""
    data = yaml.safe_load(path.read_text())
    required = {key_from_dict(d) for d in data.get("required", [])}
    optional = {key_from_dict(d) for d in data.get("optional", [])}
    return required, optional


def score_bundle(
    bundle: InvariantBundle,
    required: set[InvariantKey],
    optional: set[InvariantKey],
) -> dict[str, Any]:
    """对 Agent 产出的 bundle 打分。

    返回一个 dict：
      - recall: 命中多少 required
      - precision: 抽到的里面有多少算"对"（required ∪ optional）
      - missed: 没抽到的 required 列表
      - novel: 抽到但不在 required 也不在 optional 里的（可能是惊喜也可能噪音）
    """
    agent_keys = {key_from_invariant(inv) for inv in bundle.items}

    hit_required = agent_keys & required
    hit_optional = agent_keys & optional
    missed = required - agent_keys
    novel = agent_keys - required - optional

    recall = len(hit_required) / len(required) if required else 0.0
    # precision 的分母是 Agent 抽到的总数；分子是"命中 required 或 optional"
    precision = (
        (len(hit_required) + len(hit_optional)) / len(agent_keys)
        if agent_keys
        else 0.0
    )

    return {
        "recall": recall,
        "precision": precision,
        "hit_required_count": len(hit_required),
        "hit_optional_count": len(hit_optional),
        "missed": sorted(str(k) for k in missed),
        "novel": sorted(str(k) for k in novel),
        "total_extracted": len(agent_keys),
    }


# --- 主流程 -------------------------------------------------------------------

def run_one(doc_path: Path, run_index: int) -> tuple[DesignDocResult, float]:
    """跑一次 DesignDocAgent，返回结果和 wall-clock 秒数。"""
    session_id = f"eval-design-doc-run{run_index}"
    client = make_llm_client(session_id=session_id)
    t0 = time.perf_counter()
    result = run_design_doc_agent(doc_paths=[doc_path], llm=client)
    wall = time.perf_counter() - t0
    return result, wall


def main() -> int:
    load_dotenv()
    parser = argparse.ArgumentParser()
    parser.add_argument("--runs", type=int, default=5)
    parser.add_argument("--doc", type=str, default="docs/example_skill_v1.md")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--out", type=str, default="evals/design_doc/results.md")
    args = parser.parse_args()

    doc_path = Path(args.doc)
    golden_path = Path("evals/design_doc/golden_invariants.yaml")

    if not doc_path.exists():
        print(f"找不到策划文档：{doc_path}")
        return 1
    if not golden_path.exists():
        print(f"找不到 golden：{golden_path}")
        return 1

    required, optional = load_golden(golden_path)
    print(f"[eval] golden required={len(required)} optional={len(optional)}")

    # 粗略成本估算：单次 DesignDoc 约 30k token，在 DeepSeek 上 ~$0.05
    est_usd = args.runs * 0.05
    if args.dry_run:
        print(f"[dry-run] {args.runs} 次跑估计花费 ~${est_usd:.2f}")
        return 0

    confirm_real_run(est_usd, f"跑 {args.runs} 次 DesignDocAgent")

    runs: list[RunMetrics] = []
    details: list[dict[str, Any]] = []

    for i in range(1, args.runs + 1):
        print(f"\n[eval] Run {i}/{args.runs}...")
        try:
            result, wall = run_one(doc_path, i)
        except Exception as e:
            print(f"  ✗ 失败: {e}")
            continue

        scored = score_bundle(result.bundle, required, optional)
        metrics = RunMetrics(
            recall=scored["recall"],
            precision=scored["precision"],
            steps=result.stats.steps,
            tokens=result.stats.extra.get("total_tokens", 0) if hasattr(result.stats, "extra") else 0,
            usd=0.0,  # TODO: 从 trace 里提取
            wall_seconds=wall,
            extra={
                "hit_required": scored["hit_required_count"],
                "hit_optional": scored["hit_optional_count"],
                "total_extracted": scored["total_extracted"],
                "missed": scored["missed"],
                "novel": scored["novel"],
                "finalized_by_agent": result.finalized_by_agent,
            },
        )
        runs.append(metrics)
        details.append(scored)
        print(
            f"  recall={metrics.recall:.2%} precision={metrics.precision:.2%} "
            f"抽到 {scored['total_extracted']} 条（required {scored['hit_required_count']}/"
            f"{len(required)} + optional {scored['hit_optional_count']} + novel {len(scored['novel'])}）"
            f" steps={metrics.steps} wall={metrics.wall_seconds:.1f}s"
        )

    if not runs:
        print("所有 run 都失败了。")
        return 2

    # 汇总结果
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    md = _render_results(runs, details, required, optional, doc_path)
    out_path.write_text(md, encoding="utf-8")
    print(f"\n[eval] 结果已写入 {out_path}")
    return 0


def _render_results(
    runs: list[RunMetrics],
    details: list[dict[str, Any]],
    required: set[InvariantKey],
    optional: set[InvariantKey],
    doc_path: Path,
) -> str:
    """生成 results.md 内容。"""
    lines = [
        "# DesignDocAgent 评估结果",
        "",
        f"- 文档：`{doc_path}`",
        f"- Golden required：{len(required)} 条；optional：{len(optional)} 条",
        f"- Runs：{len(runs)}",
        "",
        render_metrics_table("各次运行", runs),
    ]

    # 漏抽统计：统计所有 run 里被漏的 invariant 及其出现次数
    missed_counter: dict[str, int] = {}
    novel_counter: dict[str, int] = {}
    for d in details:
        for k in d["missed"]:
            missed_counter[k] = missed_counter.get(k, 0) + 1
        for k in d["novel"]:
            novel_counter[k] = novel_counter.get(k, 0) + 1

    if missed_counter:
        lines.append("## 被漏抽的 required invariant")
        lines.append("")
        lines.append("| Invariant | 漏抽次数 (/N) |")
        lines.append("|---|---:|")
        for k, n in sorted(missed_counter.items(), key=lambda x: -x[1]):
            lines.append(f"| `{k}` | {n}/{len(runs)} |")
        lines.append("")

    if novel_counter:
        lines.append("## Agent 额外抽到的（不在 required / optional 里）")
        lines.append("")
        lines.append("| Invariant | 出现次数 |")
        lines.append("|---|---:|")
        for k, n in sorted(novel_counter.items(), key=lambda x: -x[1]):
            lines.append(f"| `{k}` | {n}/{len(runs)} |")
        lines.append("")

    # 最终建议
    avg_recall = sum(r.recall for r in runs) / len(runs)
    avg_prec = sum(r.precision for r in runs) / len(runs)
    lines.append("## 结论")
    lines.append("")
    if avg_recall >= 0.9 and avg_prec >= 0.85:
        verdict = "✓ 可用——召回和准确率都达标"
    elif avg_recall >= 0.75:
        verdict = "△ 能用——召回基本够，但漏抽集中在某些 kind，看 prompt 能否改进"
    else:
        verdict = "✗ 需优化——召回不足 75%"
    lines.append(verdict)
    lines.append("")

    return "\n".join(lines)


if __name__ == "__main__":
    raise SystemExit(main())
