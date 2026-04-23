"""报告层：把 TestSuiteResult / FailureBundle 渲染成人看的输出。"""
from gameguard.reports.html import render_regress_html, write_regress_html
from gameguard.reports.markdown import (
    render_bug_reports,
    render_suite_report,
    write_bug_reports,
    write_suite_report,
)
from gameguard.reports.regress import RegressDiff, RegressEntry, compute_regress_diff
from gameguard.reports.schema import (
    BugReport,
    CaseLine,
    FailureSection,
    Severity,
    SuiteReport,
    TriageOutput,
)

__all__ = [
    "BugReport",
    "CaseLine",
    "FailureSection",
    "RegressDiff",
    "RegressEntry",
    "Severity",
    "SuiteReport",
    "TriageOutput",
    "compute_regress_diff",
    "render_bug_reports",
    "render_regress_html",
    "render_suite_report",
    "write_bug_reports",
    "write_regress_html",
    "write_suite_report",
]
