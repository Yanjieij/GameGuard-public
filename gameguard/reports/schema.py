"""测试报告和 Bug 单的数据结构。

报告分两层：

  1. 测试套件报告（SuiteReport）：对应 TestSuiteResult，记"这个 plan 跑完
     的结果"——哪些用例通过、哪些失败，附带 trace 路径。D3 就需要。类似
     pytest 的 summary、Allure 的 overview、Jenkins 的 test results tab。
  2. Bug 单（BugReport）：TriageAgent 聚类失败后产出的"可提交到 Jira 的一
     行 bug"，D7 开始用，字段包括可复现命令、严重级、归因建议等。

两层刻意分：SuiteReport 是原材料（每条失败都列），BugReport 是加工成品
（多条失败可能合并成一条 bug）。D3 阶段先做 SuiteReport，BugReport 字段
先占位，D7 再扩充。
"""
from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

from gameguard.testcase.model import TestSuiteResult

# --------------------------------------------------------------------------- #
# 严重级分类（参考真实游戏团队的 Jira 配置：S0–S3）
# --------------------------------------------------------------------------- #

Severity = Literal["S0", "S1", "S2", "S3"]
SEVERITY_DESC: dict[Severity, str] = {
    "S0": "Blocker — 崩溃、数据丢失、核心功能完全不可用",
    "S1": "Critical — 主要功能异常，无合理 workaround",
    "S2": "Major — 明显功能缺陷，但有 workaround",
    "S3": "Minor — 体验/细节问题",
}

# --------------------------------------------------------------------------- #
# D3 用：套件报告
# --------------------------------------------------------------------------- #

class SuiteReport(BaseModel):
    """D3 的报告对象。一条 plan 跑完就产出一份 SuiteReport。

    与 `TestSuiteResult` 的区别：
      - TestSuiteResult：结构化原始数据（给程序消费）
      - SuiteReport    ：面向人的呈现（给 QA/工程师看）
    """

    generated_at: datetime = Field(default_factory=datetime.now)
    plan_id: str
    plan_version: str
    sandbox: str

    total: int
    passed: int
    failed: int
    errored: int
    wall_time_ms: float

    # 每条用例的一行简报（Markdown / HTML 里渲染成表格）
    case_lines: list["CaseLine"] = Field(default_factory=list)

    # 失败/错误的详细段落
    failure_sections: list["FailureSection"] = Field(default_factory=list)

    @classmethod
    def from_suite_result(cls, result: TestSuiteResult) -> "SuiteReport":
        """把 TestSuiteResult 折叠成 SuiteReport。"""
        case_lines: list[CaseLine] = []
        failures: list[FailureSection] = []
        for c in result.cases:
            case_lines.append(
                CaseLine(
                    id=c.case_id,
                    name=c.case_name,
                    outcome=c.outcome.value,
                    ticks=c.ticks_elapsed,
                    sim_time=c.sim_time,
                    wall_ms=c.wall_time_ms,
                    # 简要标记：哪些断言失败（只列 ID）
                    failing_invariants=[
                        ao.result.invariant_id for ao in c.failing_assertions
                    ],
                )
            )
            if c.failing_assertions or c.error_message:
                failures.append(
                    FailureSection(
                        case_id=c.case_id,
                        case_name=c.case_name,
                        error_message=c.error_message,
                        failing_assertions=[
                            _AssertionBullet(
                                invariant_id=ao.result.invariant_id,
                                when=ao.when.value,
                                message=ao.result.message,
                                witness_t=ao.result.witness_t,
                                witness_tick=ao.result.witness_tick,
                                actual=_safe_str(ao.result.actual),
                                expected=_safe_str(ao.result.expected),
                            )
                            for ao in c.failing_assertions
                        ],
                        trace_path=c.trace_path,
                    )
                )

        return cls(
            plan_id=result.plan_id,
            plan_version=result.plan_version,
            sandbox=result.sandbox,
            total=result.total,
            passed=result.passed,
            failed=result.failed,
            errored=result.errored,
            wall_time_ms=result.wall_time_ms,
            case_lines=case_lines,
            failure_sections=failures,
        )

class CaseLine(BaseModel):
    id: str
    name: str
    outcome: str
    ticks: int
    sim_time: float
    wall_ms: float
    failing_invariants: list[str] = Field(default_factory=list)

class _AssertionBullet(BaseModel):
    invariant_id: str
    when: str
    message: str
    witness_t: float | None = None
    witness_tick: int | None = None
    actual: str | None = None
    expected: str | None = None

class FailureSection(BaseModel):
    case_id: str
    case_name: str
    error_message: str | None = None
    failing_assertions: list[_AssertionBullet] = Field(default_factory=list)
    trace_path: str | None = None

# --------------------------------------------------------------------------- #
# D7 预留：Jira 兼容的 Bug 单
# --------------------------------------------------------------------------- #

class BugReport(BaseModel):
    """Jira-compatible bug 结构（D7 启用）。

    字段命名对齐米哈游 / 主流 AAA 团队的 Jira/飞书项目常见配置，
    让 Triage 产出的 JSON 可直接通过 webhook 提单（无额外字段映射）。

    设计原则：
      - 可序列化 —— 完整 JSON 可贴进飞书工单
      - 可追溯 —— 每条 bug 都引用 trace + snapshot 路径，工程师能本地复现
      - 可聚类 —— 通过 ``cluster_size`` / ``member_case_ids`` 反映
        TriageAgent 把多少条相关失败合并成了这一条 bug
    """

    bug_id: str
    title: str
    severity: Severity
    component: str                 # e.g. "Skill.Cooldown"
    version_introduced: str        # e.g. "pysim-v2"
    repro_steps: list[str] = Field(default_factory=list)
    expected: str = ""
    actual: str = ""
    invariant_violated: str | None = None
    regression_from: str | None = None    # 例：commit hash
    suggested_owner: str | None = None
    evidence_trace: str | None = None     # trace 文件路径
    evidence_snapshot: str | None = None  # snapshot 文件路径
    tags: list[str] = Field(default_factory=list)

    # ---- D7 新增：聚类来源 ----
    cluster_size: int = Field(
        1,
        description="这条 bug 由多少条 testcase 失败合并而来；1 表示孤例。",
    )
    member_case_ids: list[str] = Field(
        default_factory=list,
        description="参与聚类的 case_id 列表（按聚类时的相关性排序）",
    )
    representative_case_id: str = Field(
        "",
        description="代表用例的 id —— 'gameguard repro <id>' 一键复现用",
    )
    cluster_rationale: str = Field(
        "",
        description="为什么把这些用例聚到一起（规则匹配 / LLM 判断 / 单条孤例）",
    )

class TriageOutput(BaseModel):
    """TriageAgent 的整体产物：bug 列表 + 元数据。"""

    plan_id: str
    sandbox: str
    generated_at: datetime = Field(default_factory=datetime.now)
    total_failures: int      # FAILED + ERROR 用例总数（聚类前）
    total_bugs: int          # 聚类后的 BugReport 数（应 ≤ total_failures）
    bugs: list[BugReport] = Field(default_factory=list)
    # LLM 用量（便于面试讲"成本可控"）
    llm_tokens: int = 0
    llm_cost_usd: float = 0.0

# --------------------------------------------------------------------------- #
# 严重级映射表（计划文档里钦定的规则阶段映射）
# --------------------------------------------------------------------------- #

SEVERITY_BY_INVARIANT_KIND: dict[str, Severity] = {
    # 确定性破坏 = S0：直接破坏 lockstep / replay，无 workaround
    "replay_deterministic": "S0",
    # HP/MP 跌负 = S1：明显数值异常，玩家立刻能感知
    "hp_nonneg": "S1",
    "mp_nonneg": "S1",
    # 数值漂移类 = S2：可观察但有 workaround（重启技能等）
    "cooldown_at_least_after_cast": "S2",
    "buff_refresh_magnitude_stable": "S2",
    "buff_stacks_within_limit": "S2",
    "interrupt_clears_casting": "S2",
    "interrupt_refunds_mp": "S2",
    "dot_total_damage_within_tolerance": "S2",
}

DEFAULT_SEVERITY: Severity = "S2"  # 没有映射的归 S2
ERROR_SEVERITY: Severity = "S2"    # 沙箱崩 / 动作被拒 = S2 兜底

# --------------------------------------------------------------------------- #
# 小工具
# --------------------------------------------------------------------------- #

def _safe_str(value: object) -> str | None:
    """把 actual/expected 字段统一转成短字符串。失败时退回 repr。"""
    if value is None:
        return None
    try:
        return str(value)
    except Exception:  # noqa: BLE001
        return repr(value)
