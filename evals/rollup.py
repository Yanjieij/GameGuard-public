"""把各 Agent eval 的 results.md 聚合成项目根的 EVAL.md。

跑法：
    python -m evals.rollup

读：
    evals/design_doc/results.md
    evals/test_gen/results.md
    evals/triage/results.md
    evals/critic/results.md

写：
    EVAL.md       （项目根）
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path


SECTIONS = [
    ("DesignDocAgent", "evals/design_doc/results.md"),
    ("TestGenAgent", "evals/test_gen/results.md"),
    ("TriageAgent", "evals/triage/results.md"),
    ("CriticAgent", "evals/critic/results.md"),
    ("LLM Provider 对比", "evals/compare_models/results.md"),
]


def _extract_mean_row(md: str) -> str | None:
    """从 results.md 里抓出"**mean**"开头的那行，作为汇总数字的来源。

    宽松匹配：也接受 "Agent mean" 这种带前缀的变体。
    """
    for line in md.splitlines():
        low = line.lower()
        if "**mean**" in low or "agent mean" in low:
            return line.strip()
    return None


def _extract_first_heading_block(md: str) -> str:
    """跳过最顶的 H1 标题，返回剩下的内容（用来拼入 EVAL.md 不重复 H1）。"""
    lines = md.splitlines()
    # 去掉第一个 "# xxx" 标题
    start = 0
    for i, line in enumerate(lines):
        if line.startswith("# "):
            start = i + 1
            break
    # 把剩下内容里的 "##" 降级为 "###"、"###" 降级为 "####"（避免大纲层级混乱）
    demoted: list[str] = []
    for ln in lines[start:]:
        if ln.startswith("#### "):
            demoted.append("##### " + ln[5:])
        elif ln.startswith("### "):
            demoted.append("#### " + ln[4:])
        elif ln.startswith("## "):
            demoted.append("### " + ln[3:])
        else:
            demoted.append(ln)
    return "\n".join(demoted).strip()


def main() -> int:
    summaries: list[tuple[str, str, str]] = []
    details: list[tuple[str, str, str]] = []

    for name, path in SECTIONS:
        p = Path(path)
        if not p.exists():
            summaries.append((name, path, "_尚未运行_"))
            details.append((name, path, "> ⚠ 尚未运行，先跑 "
                            f"`python -m {path.replace('/', '.').replace('.md', '').replace('results', 'eval_' + name.lower().replace('agent', ''))}`\n"))
            continue
        md = p.read_text(encoding="utf-8")
        mean_row = _extract_mean_row(md) or "（无 mean 行）"
        summaries.append((name, path, mean_row))
        details.append((name, path, _extract_first_heading_block(md)))

    # 组装 EVAL.md
    today = datetime.now().strftime("%Y-%m-%d")
    lines = [
        "# GameGuard · Agent 效果评估",
        "",
        f"> 最近一次 rollup：{today}",
        "> ",
        "> 这份文件是 `evals/` 目录下 4 份 `results.md` 的汇总。",
        "> 复跑：`python -m evals.rollup`（依赖先跑各 Agent 的 eval 脚本）。",
        "",
        "## 快速一览",
        "",
        "下表是每个 Agent 评估的 mean 行（详情点对应章节）：",
        "",
    ]

    for name, path, mean_row in summaries:
        lines.append(f"### {name}")
        lines.append("")
        lines.append(f"- 详细结果：[`{path}`]({path})")
        if mean_row.startswith("|"):
            # 格式化：拿那一行当一个 mini markdown 表
            lines.append("")
            lines.append(mean_row)
        else:
            lines.append(f"- {mean_row}")
        lines.append("")

    lines.append("---")
    lines.append("")
    lines.append("## 各 Agent 详细结果")
    lines.append("")
    lines.append("下面是各 results.md 的原文拼接（标题层级降一级以便统一大纲）。")
    lines.append("")

    for name, path, body in details:
        lines.append(f"## {name}")
        lines.append("")
        lines.append(f"*来源：[`{path}`]({path})*")
        lines.append("")
        lines.append(body)
        lines.append("")
        lines.append("---")
        lines.append("")

    out = Path("EVAL.md")
    out.write_text("\n".join(lines), encoding="utf-8")
    print(f"[rollup] 写入 {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
