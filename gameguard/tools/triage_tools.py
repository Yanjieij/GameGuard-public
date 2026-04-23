"""TriageAgent 的工具集。

设计要点

TriageAgent 的工作分两阶段：
  1) 规则阶段（纯 Python）：把所有 FAILED/ERROR 用例按 (invariant kind,
     actor, skill) 等维度聚成"候选簇"。这步不调 LLM，确定性、便宜。
  2) LLM 阶段：对 size > 1 的候选簇，调用 LLM-as-judge 让模型判断
     "这些失败是同一个 bug，还是恰巧聚到一起的不同 bug"。size = 1 的
     候选簇直接归一条 BugReport，跳过 LLM。

本文件提供 LLM 在第二阶段会用到的工具：

  - ``list_failures``：列出所有候选簇 + 每簇的失败摘要（不含 trace 全文）
  - ``inspect_cluster``：拉某个候选簇的详细信息（含每条失败的关键证据）
  - ``read_trace_tail``：按需读 trace 的末 N 行（避免 prompt 爆炸）
  - ``emit_bug_report``：基于一个候选簇产出最终的 BugReport
  - ``merge_clusters``：把多个候选簇合并成一个（如果 LLM 觉得它们其实是同根）
  - ``finalize``：宣告 triage 结束

所有 LLM 看见的失败信息都是摘要级——不直接喂完整 trace。这是关键
工程经验：trace JSONL 经常 MB 级别，全喂会爆 context。我们让 LLM 主动
``read_trace_tail`` 想看就看。

为什么 BugReport 由 LLM "拼装" 而非直接从聚类输出？

聚类只能告诉我们 "这些失败是一回事"。要写出能直接进 Jira 的 bug 单，
还需要：
  - 中文化的 ``title``（"火球术冷却被切技能错误重置" 比 "I-04-fireball
    failed" 易读）
  - 自然语言的 ``repro_steps``
  - 严重级（虽然我们有规则映射表，但 LLM 偶尔需要根据现象调整）

让 LLM 在拼装时填这些"软"字段，规则代码填"硬"字段（severity 默认值、
member_case_ids、cluster_size），是 hybrid 方案的最佳点。
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path

from pydantic import BaseModel, Field

from gameguard.reports.schema import (
    DEFAULT_SEVERITY,
    ERROR_SEVERITY,
    SEVERITY_BY_INVARIANT_KIND,
    BugReport,
    Severity,
)
from gameguard.testcase.model import CaseOutcome, TestResult, TestSuiteResult
from gameguard.tools.schemas import Tool

# --- 候选簇 — 规则阶段的产物 ---
@dataclass
class FailureCluster:
    """一组被规则阶段判为"可能是同一 bug"的 TestResult。

    LLM 阶段会决定是否最终合并 / 拆开 / 直接产出 BugReport。
    """

    cluster_id: str
    cases: list[TestResult]
    # 用于 LLM 的人话摘要
    summary: str

    @property
    def size(self) -> int:
        return len(self.cases)

    @property
    def representative(self) -> TestResult:
        """挑一条代表用例：`FAILED` 优先于 `ERROR`，sim_time 短的优先。"""
        sorted_cases = sorted(
            self.cases,
            key=lambda c: (c.outcome != CaseOutcome.FAILED, c.sim_time),
        )
        return sorted_cases[0]

def cluster_failures(suite: TestSuiteResult) -> list[FailureCluster]:
    """规则阶段：根据失败的 invariant kind / actor / skill / error message 聚类。

    返回的 list 顺序稳定（按 cluster_id 排序），便于回放。
    """
    bins: dict[str, list[TestResult]] = {}

    for case in suite.cases:
        if case.outcome not in (CaseOutcome.FAILED, CaseOutcome.ERROR):
            continue

        if case.outcome == CaseOutcome.FAILED:
            # 取第一条失败断言作为聚类 key（同 case 多断言失败时通常同根）
            ao = case.failing_assertions[0] if case.failing_assertions else None
            if ao is None:
                # outcome=FAILED 但没有失败断言（罕见）：按 case_id hash
                key = f"failed-orphan-{_short_hash(case.case_id)}"
            else:
                # 通过 invariant_id 推断 kind：我们没有 InvariantBundle 可查，
                # 但 invariant_id 通常以 "I-XX-yy" 形式编码，前缀就是 kind 暗示。
                # 更稳的做法是从 InvariantResult.message 里抓关键词。
                inv_id = ao.result.invariant_id
                # invariant_id_prefix = "I-04"（按"-"切前两段）
                inv_prefix = "-".join(inv_id.split("-")[:2])
                key = f"failed-{inv_prefix}"
        else:
            # ERROR：按错误消息前 80 字符 hash
            err_short = (case.error_message or "")[:80]
            key = f"error-{_short_hash(err_short)}"

        bins.setdefault(key, []).append(case)

    clusters: list[FailureCluster] = []
    for cid, cases in sorted(bins.items()):
        clusters.append(
            FailureCluster(
                cluster_id=cid,
                cases=cases,
                summary=_describe_cluster(cid, cases),
            )
        )
    return clusters

def _describe_cluster(cid: str, cases: list[TestResult]) -> str:
    """生成给 LLM 看的一行人话摘要。"""
    if cases[0].outcome == CaseOutcome.ERROR:
        msg = (cases[0].error_message or "").splitlines()[0][:100]
        return f"{len(cases)} 条 ERROR；首例错误：{msg}"
    # FAILED
    aos = cases[0].failing_assertions
    if not aos:
        return f"{len(cases)} 条 FAILED（无具体断言）"
    inv_id = aos[0].result.invariant_id
    msg = aos[0].result.message[:120]
    return f"{len(cases)} 条 FAILED on `{inv_id}`：{msg}"

def _short_hash(s: str) -> str:
    return hashlib.md5(s.encode("utf-8")).hexdigest()[:8]

# --- Triage 上下文 —— 工具的 backing state ---
@dataclass
class TriageContext:
    """TriageAgent 运行期的状态。"""

    suite: TestSuiteResult
    clusters: list[FailureCluster]
    # 索引：cluster_id -> FailureCluster
    by_id: dict[str, FailureCluster] = field(default_factory=dict)
    # LLM 产出的 bug 报告（按 emit 顺序）
    bugs: list[BugReport] = field(default_factory=list)
    # 已经被 emit 过的 cluster id（避免重复 emit）
    emitted_clusters: set[str] = field(default_factory=set)
    # 合并历史：merged_id -> [被合并的原 cluster_ids]
    merged_into: dict[str, list[str]] = field(default_factory=dict)
    finalized: bool = False
    finalize_reason: str = ""

    def __post_init__(self) -> None:
        self.by_id = {c.cluster_id: c for c in self.clusters}

# --- 工具的 I/O Schema ---
class _NoInput(BaseModel):
    pass

class ClusterSummary(BaseModel):
    cluster_id: str
    size: int
    summary: str
    case_ids: list[str]
    representative_case_id: str

class ListFailuresOutput(BaseModel):
    total_failures: int
    cluster_count: int
    clusters: list[ClusterSummary]

class InspectClusterInput(BaseModel):
    cluster_id: str = Field(..., description="要查看的候选簇 ID（list_failures 返回值）")

class FailureDetail(BaseModel):
    case_id: str
    case_name: str
    outcome: str
    sandbox: str
    seed: int
    sim_time: float
    error_message: str | None = None
    failing_assertions: list[dict] = Field(default_factory=list)
    trace_path: str | None = None

class InspectClusterOutput(BaseModel):
    cluster_id: str
    summary: str
    failures: list[FailureDetail]

class ReadTraceTailInput(BaseModel):
    case_id: str = Field(..., description="要查看 trace 的 case_id（来自 inspect_cluster）")
    n_lines: int = Field(20, ge=1, le=200, description="末尾读多少行 JSONL；默认 20")

class TraceTailOutput(BaseModel):
    case_id: str
    trace_path: str
    total_lines: int
    tail_lines: list[str]

class EmitBugReportInput(BaseModel):
    """LLM 用此工具拼装最终 BugReport。

    必填字段较多，但都很简短；强制结构化让产出可直接进 Jira。
    """

    cluster_id: str = Field(..., description="该 bug 由哪个候选簇产生")
    title: str = Field(..., description="中文短标题，例如 '切换技能时火球术冷却被错误重置'")
    component: str = Field(
        ...,
        description="影响子系统，按 'X.Y' 格式写。例：'Skill.Cooldown' / 'Skill.Buff' / 'Skill.StateMachine'",
    )
    severity: Severity | None = Field(
        None,
        description="严重级 S0-S3。不传则按规则映射表自动决定。",
    )
    repro_steps: list[str] = Field(
        ...,
        description="自然语言复现步骤，3-6 条。例：['用 seed=42 重置沙箱', 'p1 释放 skill_fireball at t=0', ...]",
    )
    expected: str = Field(..., description="正确表现的简短描述")
    actual: str = Field(..., description="实际表现的简短描述")
    suggested_owner: str | None = Field(
        None, description="建议的归属团队/模块，例 'skill_system_team'"
    )
    tags: list[str] = Field(default_factory=list, description="例 ['regression','BUG-001']")
    rationale: str = Field(
        "",
        description="（可选）你为什么把这些用例聚成一条 bug 的简短说明",
    )

class EmitBugReportOutput(BaseModel):
    ok: bool
    bug_id: str
    error: str | None = None

class MergeClustersInput(BaseModel):
    cluster_ids: list[str] = Field(..., min_length=2, description="要合并的两个或多个 cluster_id")
    new_cluster_id: str | None = Field(
        None,
        description="合并后的新簇 ID。不传时自动用第一个 cluster_id 作为合并目标。",
    )

class MergeClustersOutput(BaseModel):
    ok: bool
    new_cluster_id: str
    member_count: int
    error: str | None = None

class FinalizeInput(BaseModel):
    reason: str = "done"

class FinalizeOutput(BaseModel):
    ok: bool = True
    bug_count: int = 0
    message: str = "ok"

# --- 工具工厂 ---
def build_triage_tools(ctx: TriageContext) -> list[Tool]:
    return [
        _build_list_failures(ctx),
        _build_inspect_cluster(ctx),
        _build_read_trace_tail(ctx),
        _build_emit_bug_report(ctx),
        _build_merge_clusters(ctx),
        _build_finalize(ctx),
    ]

def _build_list_failures(ctx: TriageContext) -> Tool:
    def _fn(_: _NoInput) -> ListFailuresOutput:
        summaries: list[ClusterSummary] = []
        for c in ctx.clusters:
            summaries.append(
                ClusterSummary(
                    cluster_id=c.cluster_id,
                    size=c.size,
                    summary=c.summary,
                    case_ids=[r.case_id for r in c.cases],
                    representative_case_id=c.representative.case_id,
                )
            )
        return ListFailuresOutput(
            total_failures=sum(c.size for c in ctx.clusters),
            cluster_count=len(ctx.clusters),
            clusters=summaries,
        )

    return Tool(
        name="list_failures",
        description=(
            "列出所有候选簇（规则聚类后的失败分组）。先调用此工具了解全貌；"
            "如果某簇 size > 1，再 inspect_cluster 看具体失败。size==1 的"
            "簇可以直接 emit_bug_report 而不必 inspect。"
        ),
        input_model=_NoInput,
        fn=_fn,
    )

def _build_inspect_cluster(ctx: TriageContext) -> Tool:
    def _fn(args: InspectClusterInput) -> InspectClusterOutput:
        c = ctx.by_id.get(args.cluster_id)
        if c is None:
            raise ValueError(
                f"未知 cluster_id={args.cluster_id!r}。可用："
                f"{list(ctx.by_id)[:10]}{'...' if len(ctx.by_id) > 10 else ''}"
            )
        details: list[FailureDetail] = []
        for r in c.cases:
            details.append(
                FailureDetail(
                    case_id=r.case_id,
                    case_name=r.case_name,
                    outcome=r.outcome.value,
                    sandbox=r.sandbox,
                    seed=r.seed,
                    sim_time=r.sim_time,
                    error_message=r.error_message,
                    failing_assertions=[
                        {
                            "invariant_id": ao.result.invariant_id,
                            "when": ao.when.value,
                            "message": ao.result.message,
                            "witness_t": ao.result.witness_t,
                            "actual": _safe_str(ao.result.actual),
                            "expected": _safe_str(ao.result.expected),
                        }
                        for ao in r.failing_assertions
                    ],
                    trace_path=r.trace_path,
                )
            )
        return InspectClusterOutput(
            cluster_id=c.cluster_id, summary=c.summary, failures=details
        )

    return Tool(
        name="inspect_cluster",
        description=(
            "查看某个候选簇里**每条失败**的详细信息（断言数值、错误消息、"
            "trace 路径）。在 emit_bug_report 之前先 inspect 你想拼装的簇。"
        ),
        input_model=InspectClusterInput,
        fn=_fn,
    )

def _build_read_trace_tail(ctx: TriageContext) -> Tool:
    def _fn(args: ReadTraceTailInput) -> TraceTailOutput:
        # 在 ctx 里找 case
        case = next(
            (
                r
                for c in ctx.clusters
                for r in c.cases
                if r.case_id == args.case_id
            ),
            None,
        )
        if case is None or case.trace_path is None:
            raise ValueError(f"找不到 case_id={args.case_id!r} 的 trace 文件")
        p = Path(case.trace_path)
        if not p.exists():
            raise FileNotFoundError(f"trace 文件不存在：{p}")
        all_lines = p.read_text(encoding="utf-8").splitlines()
        return TraceTailOutput(
            case_id=args.case_id,
            trace_path=str(p),
            total_lines=len(all_lines),
            tail_lines=all_lines[-args.n_lines :],
        )

    return Tool(
        name="read_trace_tail",
        description=(
            "读取指定用例的 trace（JSONL）末尾若干行。仅在你需要看具体事件"
            "序列才调用——trace 文件可能很长，不要一次读全部。默认末 20 行。"
        ),
        input_model=ReadTraceTailInput,
        fn=_fn,
    )

def _build_emit_bug_report(ctx: TriageContext) -> Tool:
    def _fn(args: EmitBugReportInput) -> EmitBugReportOutput:
        c = ctx.by_id.get(args.cluster_id)
        if c is None:
            return EmitBugReportOutput(
                ok=False, bug_id="", error=f"未知 cluster_id={args.cluster_id!r}"
            )
        if args.cluster_id in ctx.emitted_clusters:
            return EmitBugReportOutput(
                ok=False,
                bug_id="",
                error=f"cluster {args.cluster_id} 已经 emit 过 bug，不要重复",
            )

        rep = c.representative
        # 决定 invariant_violated + 默认 severity
        invariant_violated = None
        severity = args.severity
        if rep.failing_assertions:
            ao = rep.failing_assertions[0]
            invariant_violated = ao.result.invariant_id
            if severity is None:
                # 由 invariant_id 反推 kind 比较脆；优先按 message 关键字
                # 但目前规则映射表用 kind 做 key，我们退化到从已知前缀映射
                inferred_kind = _infer_kind_from_id(ao.result.invariant_id)
                severity = SEVERITY_BY_INVARIANT_KIND.get(
                    inferred_kind, DEFAULT_SEVERITY
                )
        elif rep.error_message and severity is None:
            severity = ERROR_SEVERITY

        if severity is None:
            severity = DEFAULT_SEVERITY

        bug_id = f"GG-{_short_hash(args.cluster_id + rep.case_id)}-{len(ctx.bugs) + 1:03d}"
        bug = BugReport(
            bug_id=bug_id,
            title=args.title,
            severity=severity,
            component=args.component,
            version_introduced=ctx.suite.sandbox,
            repro_steps=args.repro_steps,
            expected=args.expected,
            actual=args.actual,
            invariant_violated=invariant_violated,
            suggested_owner=args.suggested_owner,
            evidence_trace=rep.trace_path,
            tags=args.tags,
            cluster_size=c.size,
            member_case_ids=[r.case_id for r in c.cases],
            representative_case_id=rep.case_id,
            cluster_rationale=args.rationale or "（未填）",
        )
        ctx.bugs.append(bug)
        ctx.emitted_clusters.add(args.cluster_id)
        return EmitBugReportOutput(ok=True, bug_id=bug_id)

    return Tool(
        name="emit_bug_report",
        description=(
            "为某个候选簇产出一条 Jira-compatible 的 BugReport。每个 cluster"
            "只能 emit 一次。severity 不传时按规则映射表（cooldown/buff -> S2，"
            "hp/mp_nonneg -> S1，determinism -> S0）。"
        ),
        input_model=EmitBugReportInput,
        fn=_fn,
    )

def _build_merge_clusters(ctx: TriageContext) -> Tool:
    def _fn(args: MergeClustersInput) -> MergeClustersOutput:
        unknown = [cid for cid in args.cluster_ids if cid not in ctx.by_id]
        if unknown:
            return MergeClustersOutput(
                ok=False, new_cluster_id="", member_count=0,
                error=f"未知 cluster_id：{unknown}",
            )
        target_id = args.new_cluster_id or args.cluster_ids[0]
        merged_cases: list[TestResult] = []
        for cid in args.cluster_ids:
            merged_cases.extend(ctx.by_id[cid].cases)
        # 创建合并簇
        merged = FailureCluster(
            cluster_id=target_id,
            cases=merged_cases,
            summary=(
                f"[merged from {','.join(args.cluster_ids)}] "
                + ctx.by_id[args.cluster_ids[0]].summary
            ),
        )
        # 替换 ctx.by_id：先去掉所有被合并的，再加新的
        for cid in args.cluster_ids:
            ctx.by_id.pop(cid, None)
            ctx.clusters = [c for c in ctx.clusters if c.cluster_id != cid]
        ctx.by_id[target_id] = merged
        ctx.clusters.append(merged)
        ctx.merged_into[target_id] = list(args.cluster_ids)
        return MergeClustersOutput(
            ok=True, new_cluster_id=target_id, member_count=len(merged_cases)
        )

    return Tool(
        name="merge_clusters",
        description=(
            "把多个候选簇合并成一个。仅在你确信它们是同一 bug 的不同表象"
            "时调用——合并后将作为单条 BugReport 提交。**不要随意合并**："
            "宁可分散提单也不要漏了不同 bug。"
        ),
        input_model=MergeClustersInput,
        fn=_fn,
    )

def _build_finalize(ctx: TriageContext) -> Tool:
    def _fn(args: FinalizeInput) -> FinalizeOutput:
        ctx.finalized = True
        ctx.finalize_reason = args.reason
        return FinalizeOutput(bug_count=len(ctx.bugs), message=args.reason or "ok")

    return Tool(
        name="finalize",
        description=(
            "当所有候选簇都已经 emit 过 BugReport（或被 merge 进其它）时调用。"
            "调用前请用 list_failures 检查是否还有未处理的 cluster。"
        ),
        input_model=FinalizeInput,
        fn=_fn,
    )

# --- 工具函数 ---
# 已知 invariant id 前缀 → kind 的反查表（与策划文档 I-XX 编号约定对齐）
# 这是个规则启发，不准确时 LLM 还可在 emit_bug_report 时显式传 severity
_KIND_BY_INVARIANT_ID_PREFIX: dict[str, str] = {
    "I-01": "hp_nonneg",
    "I-02": "mp_nonneg",
    "I-03": "cooldown_at_least_after_cast",
    "I-04": "cooldown_at_least_after_cast",
    "I-05": "buff_refresh_magnitude_stable",
    "I-06": "buff_stacks_within_limit",
    "I-07": "interrupt_clears_casting",
    "I-08": "interrupt_refunds_mp",
    "I-09": "dot_total_damage_within_tolerance",
    "I-10": "replay_deterministic",
}

def _infer_kind_from_id(inv_id: str) -> str:
    prefix = "-".join(inv_id.split("-")[:2])
    return _KIND_BY_INVARIANT_ID_PREFIX.get(prefix, "")

def _safe_str(value: object) -> str | None:
    if value is None:
        return None
    try:
        return str(value)
    except Exception:  # noqa: BLE001
        return repr(value)
