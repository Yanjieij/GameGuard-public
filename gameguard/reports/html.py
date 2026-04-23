"""HTML 报告渲染（Jinja2，零外部依赖）。

==============================================================================
为什么 HTML 而非 Markdown
==============================================================================

- Markdown 报告（D3 reports/markdown.py）适合贴飞书 / GitHub issue
- HTML 报告（本模块，D9）适合：
  - 在浏览器里**点开 trace**链接（Markdown 不支持）
  - 折叠展开 bug 详情（详尽不冗长）
  - 颜色/图标即时识别 NEW vs FIXED vs STABLE
  - CI 系统直接挂出（Jenkins / GitLab Pages）

我们刻意**不接 Allure**：Allure 需要 Java + 独立 CLI 来 build HTML，对单
模块项目过重。Jinja2 + 内嵌 CSS 实现等价 90% 体验，零依赖。

模板位于 ``gameguard/reports/templates/*.j2``。
"""
from __future__ import annotations

from pathlib import Path

from jinja2 import Environment, PackageLoader, select_autoescape

from gameguard.reports.regress import RegressDiff
from gameguard.reports.schema import TriageOutput


# 全局 Jinja2 environment：用 PackageLoader 让模板随包安装一起走
_env = Environment(
    loader=PackageLoader("gameguard.reports", "templates"),
    autoescape=select_autoescape(["html", "j2"]),
)


def render_regress_html(
    diff: RegressDiff,
    triage: TriageOutput | None = None,
) -> str:
    """渲染差分回归报告。

    triage 可选：如果提供，模板会把 NEW failures 对应的 BugReport 折叠在
    NEW 表格之后。
    """
    tpl = _env.get_template("regress.html.j2")
    return tpl.render(diff=diff, triage=triage)


def write_regress_html(
    diff: RegressDiff,
    path: str | Path,
    triage: TriageOutput | None = None,
) -> Path:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(render_regress_html(diff, triage=triage), encoding="utf-8")
    return p
