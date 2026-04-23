"""Markdown 报告渲染。

==============================================================================
为什么在 D3 先做 Markdown 而不是 HTML？
==============================================================================

  1) 面试场景下 `cat report.md` 就能秀出来，不依赖浏览器。
  2) Markdown 是 README / 飞书 / GitHub Issue 的 lingua franca；
     直接把报告贴进工单里是零成本的。
  3) HTML（Allure 风格）放到 D9 再做，那时回归对比表格更复杂，
     Markdown 不够用，HTML + Jinja 才划算。

本模块**不使用模板引擎**：仅用原生 f-string / 字符串拼接。原因：
  - Markdown 比 HTML 简单，模板引入的依赖不值得。
  - 输出结果可读性更高（IDE 里可直接阅读源码）。
  D9 的 HTML 报告我们会用 Jinja2，到时模板化更自然。
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path

from gameguard.reports.schema import (
    BugReport,
    FailureSection,
    SuiteReport,
    TriageOutput,
)


# --------------------------------------------------------------------------- #
# 公共入口
# --------------------------------------------------------------------------- #


def render_suite_report(report: SuiteReport) -> str:
    """把 SuiteReport 渲染为 Markdown 字符串。"""
    out: list[str] = []
    out.append(_render_header(report))
    out.append(_render_summary_table(report))
    out.append(_render_cases_table(report))
    if report.failure_sections:
        out.append("\n## 失败与错误详情\n")
        for section in report.failure_sections:
            out.append(_render_failure_section(section))
    out.append(_render_footer(report))
    return "\n".join(out).rstrip() + "\n"


def write_suite_report(report: SuiteReport, path: str | Path) -> Path:
    """渲染并落盘，返回 Path。"""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(render_suite_report(report), encoding="utf-8")
    return p


# --------------------------------------------------------------------------- #
# 分段渲染
# --------------------------------------------------------------------------- #


def _render_header(r: SuiteReport) -> str:
    # 顶部用 emoji 级的状态标记，方便 1 秒判断绿/红
    status_icon = "✅" if r.failed == 0 and r.errored == 0 else "❌"
    return (
        f"# {status_icon} GameGuard 测试报告\n\n"
        f"- **Plan**: `{r.plan_id}` @ version `{r.plan_version}`\n"
        f"- **Sandbox**: `{r.sandbox}`\n"
        f"- **生成时间**: {_fmt_time(r.generated_at)}\n"
    )


def _render_summary_table(r: SuiteReport) -> str:
    return (
        "\n## 概览\n\n"
        "| 总数 | 通过 | 失败 | 错误 | 墙钟耗时 |\n"
        "|---:|---:|---:|---:|---:|\n"
        f"| {r.total} | {r.passed} | {r.failed} | {r.errored} | "
        f"{r.wall_time_ms:.0f} ms |\n"
    )


def _render_cases_table(r: SuiteReport) -> str:
    lines: list[str] = [
        "\n## 用例一览\n",
        "| ID | 名称 | 结果 | sim 时间 (s) | ticks | 墙钟 (ms) | 失败断言 |",
        "|---|---|---|---:|---:|---:|---|",
    ]
    for c in r.case_lines:
        outcome_cell = _fmt_outcome(c.outcome)
        failing = ", ".join(f"`{i}`" for i in c.failing_invariants) or "—"
        lines.append(
            f"| `{c.id}` | {c.name} | {outcome_cell} | "
            f"{c.sim_time:.2f} | {c.ticks} | {c.wall_ms:.1f} | {failing} |"
        )
    return "\n".join(lines) + "\n"


def _render_failure_section(s: FailureSection) -> str:
    parts: list[str] = [f"\n### ❌ `{s.case_id}` — {s.case_name}\n"]

    if s.error_message:
        # 执行期异常：用 Markdown 的代码块展示，保留原始换行
        parts.append("**执行错误**\n")
        parts.append("```")
        parts.append(s.error_message.strip())
        parts.append("```\n")

    if s.failing_assertions:
        parts.append("**违反的不变式**\n")
        for a in s.failing_assertions:
            witness = _fmt_witness(a.witness_t, a.witness_tick)
            parts.append(
                f"- `{a.invariant_id}` ({a.when}){witness}\n"
                f"  - {a.message}\n"
                + (f"  - 期望：`{a.expected}`；实际：`{a.actual}`\n"
                   if (a.expected is not None or a.actual is not None) else "")
            )

    if s.trace_path:
        parts.append(f"\n**证据 trace**: `{s.trace_path}`\n")

    return "\n".join(parts)


def _render_footer(r: SuiteReport) -> str:
    # 这里留一个"如何复现"的提示，接下来 Triage 会把这个块扩写成
    # 结构化的 `gameguard repro <bug_id>` 命令。
    return (
        "\n---\n\n"
        "*失败用例的完整 trace 位于 `artifacts/traces/`；snapshot 位于 "
        "`artifacts/snapshots/`。可结合 `gameguard repro <case_id>`（规划中）"
        "一键复现具体失败。*\n"
    )


# --------------------------------------------------------------------------- #
# 格式化小工具
# --------------------------------------------------------------------------- #


def _fmt_time(dt: datetime) -> str:
    # ISO 近似，去掉微秒
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def _fmt_outcome(raw: str) -> str:
    """把 outcome 字符串换成带图标的 markdown 单元。"""
    return {
        "passed": "✅ passed",
        "failed": "❌ failed",
        "error": "⚠️ error",
        "skipped": "⏭ skipped",
    }.get(raw, raw)


def _fmt_witness(t: float | None, tick: int | None) -> str:
    """把 witness 时间渲染成 `" (t=1.50, tick=30)"` 或空串。"""
    if t is None and tick is None:
        return ""
    pieces: list[str] = []
    if t is not None:
        pieces.append(f"t={t:.2f}")
    if tick is not None:
        pieces.append(f"tick={tick}")
    return " (" + ", ".join(pieces) + ")"


# --------------------------------------------------------------------------- #
# Bug 报告渲染（D7）
# --------------------------------------------------------------------------- #


_SEVERITY_BADGE: dict[str, str] = {
    "S0": "🚨 **S0 Blocker**",
    "S1": "🔴 **S1 Critical**",
    "S2": "🟠 **S2 Major**",
    "S3": "🟡 **S3 Minor**",
}


def render_bug_reports(triage: TriageOutput) -> str:
    """把 TriageAgent 产出的 BugReport 列表渲染为 Markdown。

    输出风格仿 Jira 工单 + 飞书 issue summary，方便直接贴。
    """
    parts: list[str] = []
    parts.append(_render_triage_header(triage))
    if not triage.bugs:
        parts.append("\n_本套件没有产出 bug 单（无失败 / 无错误）。_\n")
        return "\n".join(parts).rstrip() + "\n"
    parts.append(_render_triage_summary_table(triage))
    parts.append("\n## Bug 详情\n")
    for bug in triage.bugs:
        parts.append(_render_one_bug(bug))
    parts.append(_render_triage_footer(triage))
    return "\n".join(parts).rstrip() + "\n"


def write_bug_reports(triage: TriageOutput, path: str | Path) -> Path:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(render_bug_reports(triage), encoding="utf-8")
    return p


def _render_triage_header(t: TriageOutput) -> str:
    return (
        f"# 🐛 GameGuard Bug 报告\n\n"
        f"- **Plan**: `{t.plan_id}` · **Sandbox**: `{t.sandbox}`\n"
        f"- **生成时间**: {_fmt_time(t.generated_at)}\n"
        f"- **失败用例**: {t.total_failures} → **聚类后 bug 数**: {t.total_bugs}\n"
        f"- **LLM 用量**: {t.llm_tokens} tokens · ${t.llm_cost_usd:.4f}\n"
    )


def _render_triage_summary_table(t: TriageOutput) -> str:
    rows = ["\n## 概览\n", "| Bug ID | 严重级 | 组件 | 标题 | 影响用例数 |",
            "|---|---|---|---|---:|"]
    for b in t.bugs:
        rows.append(
            f"| `{b.bug_id}` | {_SEVERITY_BADGE.get(b.severity, b.severity)} | "
            f"`{b.component}` | {b.title} | {b.cluster_size} |"
        )
    return "\n".join(rows) + "\n"


def _render_one_bug(b: BugReport) -> str:
    parts: list[str] = []
    parts.append(f"\n### {_SEVERITY_BADGE.get(b.severity, b.severity)} `{b.bug_id}` — {b.title}\n")
    parts.append(f"- **组件**: `{b.component}`")
    parts.append(f"- **引入版本**: `{b.version_introduced}`")
    if b.invariant_violated:
        parts.append(f"- **违反不变式**: `{b.invariant_violated}`")
    if b.suggested_owner:
        parts.append(f"- **建议归属**: `{b.suggested_owner}`")
    if b.tags:
        parts.append("- **标签**: " + ", ".join(f"`{t}`" for t in b.tags))
    parts.append(f"- **聚类来源**: 由 {b.cluster_size} 条相关失败合并；代表用例 `{b.representative_case_id}`")
    if b.cluster_rationale:
        parts.append(f"  - 聚类理由：{b.cluster_rationale}")

    parts.append("\n**期望 vs 实际**\n")
    parts.append(f"- 期望：{b.expected}")
    parts.append(f"- 实际：{b.actual}")

    parts.append("\n**复现步骤**\n")
    for i, step in enumerate(b.repro_steps, 1):
        parts.append(f"{i}. {step}")

    if b.evidence_trace:
        parts.append(f"\n**证据**: trace = `{b.evidence_trace}`")
    if b.evidence_snapshot:
        parts.append(f"，snapshot = `{b.evidence_snapshot}`")

    if b.member_case_ids and len(b.member_case_ids) > 1:
        parts.append(f"\n<details><summary>所有相关用例（{len(b.member_case_ids)}）</summary>\n")
        for cid in b.member_case_ids:
            parts.append(f"- `{cid}`")
        parts.append("</details>\n")

    return "\n".join(parts) + "\n"


def _render_triage_footer(t: TriageOutput) -> str:
    return (
        "\n---\n\n"
        "*所有 bug 已按 Jira-compatible schema 输出，可直接通过 webhook 提单。*\n"
        "*完整 trace 位于 `artifacts/traces/`；snapshot 位于 `artifacts/snapshots/`。*\n"
    )
