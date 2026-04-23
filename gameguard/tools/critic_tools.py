"""CriticAgent 的工具集（D10 stretch）。

设计要点

CriticAgent 是 ExploratoryAgent / TestGenAgent 之后的"第三方 review"
环节。它不生成新用例——只对现有 plan 里每条 case 做：

  - 静态校验：MP / CD / cast_time / wait 时长是否合理
  - assertion 引用合理性：invariant_id 是否真存在、actor/skill 是否
    匹配
  - patch 或 drop 决策：明显有错的用例直接 drop；可救的 patch（如把
    wait 时间拉长）

工具集：
  - ``list_cases``：拿到当前 plan 的所有 case 摘要
  - ``inspect_case``：拉单条 case 的完整动作 + 断言
  - ``static_check``：跑 Python 静态分析返回 issues 列表
  - ``patch_case``：替换某 case 的 actions / assertions
  - ``drop_case``：删除某条 case
  - ``finalize``：宣告 review 结束

为什么静态校验放在工具里而不是 prompt 里？

LLM 拍脑袋判 "MP 够不够" 不可靠（容易算错）；而我们已经知道 SkillBook 的
真实数据。用 Python 跑静态校验，返回结构化 issue 列表，让 LLM 基于真实数据
决策（patch / drop / accept），而不是计算。这是最佳实践。
"""
from __future__ import annotations

from dataclasses import dataclass, field

from pydantic import BaseModel, Field

from gameguard.domain.action import CastAction, InterruptAction, NoopAction, WaitAction
from gameguard.domain.character import Character
from gameguard.domain.skill import SkillBook
from gameguard.testcase.model import TestCase, TestPlan
from gameguard.tools.schemas import Tool

# --- 静态校验：跑 Python 模拟一遍纯计算（不真跑沙箱），找出"会被 sandbox 拒绝"的 case ---
@dataclass
class StaticIssue:
    """静态分析发现的一条问题。"""

    case_id: str
    severity: str          # "warn" / "error"
    code: str              # "insufficient_mp" / "cd_violation" / "interrupt_no_cast" / ...
    message: str
    action_index: int | None = None

def static_check_case(
    case: TestCase,
    skill_book: SkillBook,
    initial_characters: list[Character],
) -> list[StaticIssue]:
    """对一条 case 跑纯 Python 静态校验。

    模拟 MP / CD / state（不跑 sandbox 真 tick），按 sandbox 规则判断每个
    action 是否会被拒绝。返回的 issues 是工程级 hint，CriticAgent 据此决策。
    """
    issues: list[StaticIssue] = []

    # 复制角色状态（仅 MP / cooldowns / casting_skill）
    chars: dict[str, dict] = {
        c.id: {
            "mp": c.mp,
            "mp_max": c.mp_max,
            "cooldowns": {},          # skill_id -> seconds remaining
            "casting": None,
            "cast_remaining": 0.0,
            "alive": True,
        }
        for c in initial_characters
    }
    t = 0.0
    tick_dt = 0.05

    for i, action in enumerate(case.actions):
        if isinstance(action, CastAction):
            # 三连判断：actor 存在 / 在 cooldown 之外 / MP 够
            if action.actor not in chars:
                issues.append(StaticIssue(
                    case.id, "error", "unknown_actor",
                    f"actor {action.actor!r} 不在初始角色名单",
                    action_index=i,
                ))
                continue
            if action.skill not in skill_book.specs:
                issues.append(StaticIssue(
                    case.id, "error", "unknown_skill",
                    f"skill {action.skill!r} 不在 SkillBook 中",
                    action_index=i,
                ))
                continue
            spec = skill_book.specs[action.skill]
            ch = chars[action.actor]
            if ch["casting"] is not None:
                issues.append(StaticIssue(
                    case.id, "warn", "casting_overlap",
                    f"{action.actor} 还在 cast {ch['casting']!r} 时再开新 cast；"
                    "沙箱将拒绝。",
                    action_index=i,
                ))
                continue
            cd_remaining = ch["cooldowns"].get(action.skill, 0.0)
            if cd_remaining > 0:
                issues.append(StaticIssue(
                    case.id, "error", "cd_violation",
                    f"在 t={t:.2f} 时 {action.skill} 还有 {cd_remaining:.2f}s "
                    f"冷却（妨碍施放）",
                    action_index=i,
                ))
                continue
            if ch["mp"] < spec.mp_cost:
                issues.append(StaticIssue(
                    case.id, "error", "insufficient_mp",
                    f"在 t={t:.2f} 时 {action.actor} 只剩 {ch['mp']:.0f} MP，"
                    f"不够施 {action.skill}（需要 {spec.mp_cost}）",
                    action_index=i,
                ))
                continue
            # 模拟扣 MP + 进入 casting + 在 cast_time 后开始 CD
            ch["mp"] -= spec.mp_cost
            ch["casting"] = action.skill
            ch["cast_remaining"] = max(spec.cast_time, tick_dt)
            # cast_time 立刻完成（简化：cd_at_complete = t + cast_time）
            cd_start_t = t + max(spec.cast_time, tick_dt)
            ch["cooldowns"][action.skill] = max(
                ch["cooldowns"].get(action.skill, 0.0),
                spec.cooldown,
            )
            ch["casting"] = None  # 静态分析不 strict 模拟 cast 进行中
            ch["cast_remaining"] = 0.0
            t = cd_start_t
        elif isinstance(action, WaitAction):
            if action.seconds <= 0:
                issues.append(StaticIssue(
                    case.id, "warn", "zero_wait",
                    "wait 时长 ≤ 0，无意义",
                    action_index=i,
                ))
                continue
            t += action.seconds
            for ch in chars.values():
                for sid in list(ch["cooldowns"].keys()):
                    ch["cooldowns"][sid] = max(0.0, ch["cooldowns"][sid] - action.seconds)
                    if ch["cooldowns"][sid] == 0:
                        del ch["cooldowns"][sid]
        elif isinstance(action, InterruptAction):
            if action.actor not in chars:
                issues.append(StaticIssue(
                    case.id, "error", "unknown_actor",
                    f"interrupt 的 actor {action.actor!r} 不存在",
                    action_index=i,
                ))
                continue
            ch = chars[action.actor]
            if ch["casting"] is None:
                issues.append(StaticIssue(
                    case.id, "warn", "interrupt_no_cast",
                    f"在 t={t:.2f} interrupt 时 {action.actor} 并未 casting，"
                    "沙箱将拒绝。",
                    action_index=i,
                ))
                continue
            t += tick_dt
        elif isinstance(action, NoopAction):
            t += tick_dt
        # 其它类型不分析
    return issues

# --- 上下文与 Tool I/O Schema ---
@dataclass
class CriticContext:
    plan: TestPlan
    skill_book: SkillBook
    initial_characters: list[Character]
    # 每条 case 的静态 issues（首次拉时计算并缓存）
    issues_cache: dict[str, list[StaticIssue]] = field(default_factory=dict)
    # 决策记录
    dropped: list[str] = field(default_factory=list)
    patched: dict[str, str] = field(default_factory=dict)  # case_id -> reason
    accepted: set[str] = field(default_factory=set)
    finalized: bool = False
    finalize_reason: str = ""

class _NoInput(BaseModel):
    pass

class CaseSummary(BaseModel):
    case_id: str
    name: str
    n_actions: int
    n_assertions: int
    static_issue_count: int
    has_errors: bool

class ListCasesOutput(BaseModel):
    total: int
    cases: list[CaseSummary]

class InspectCaseInput(BaseModel):
    case_id: str

class IssueDetail(BaseModel):
    severity: str
    code: str
    message: str
    action_index: int | None = None

class InspectCaseOutput(BaseModel):
    case_id: str
    name: str
    description: str
    actions: list[dict]
    assertion_invariant_ids: list[str]
    issues: list[IssueDetail]

class PatchCaseInput(BaseModel):
    case_id: str = Field(...)
    new_actions: list[dict] = Field(
        ...,
        description="替换后的 actions 列表（kind+actor+skill+target+seconds，扁平 dict）",
    )
    rationale: str = Field(..., description="为什么这样改")

class PatchCaseOutput(BaseModel):
    ok: bool
    case_id: str
    error: str | None = None

class DropCaseInput(BaseModel):
    case_id: str
    reason: str

class DropCaseOutput(BaseModel):
    ok: bool
    case_id: str
    error: str | None = None

class AcceptCaseInput(BaseModel):
    case_id: str

class FinalizeInput(BaseModel):
    reason: str = "done"

class FinalizeOutput(BaseModel):
    ok: bool = True
    accepted: int = 0
    patched: int = 0
    dropped: int = 0

# --- Tool 工厂 ---
def build_critic_tools(ctx: CriticContext) -> list[Tool]:
    return [
        _build_list_cases(ctx),
        _build_inspect_case(ctx),
        _build_patch_case(ctx),
        _build_drop_case(ctx),
        _build_accept_case(ctx),
        _build_finalize(ctx),
    ]

def _ensure_issues(ctx: CriticContext, case_id: str) -> list[StaticIssue]:
    if case_id not in ctx.issues_cache:
        case = next((c for c in ctx.plan.cases if c.id == case_id), None)
        if case is None:
            return []
        ctx.issues_cache[case_id] = static_check_case(
            case, ctx.skill_book, ctx.initial_characters
        )
    return ctx.issues_cache[case_id]

def _build_list_cases(ctx: CriticContext) -> Tool:
    def _fn(_: _NoInput) -> ListCasesOutput:
        summaries = []
        for c in ctx.plan.cases:
            issues = _ensure_issues(ctx, c.id)
            summaries.append(CaseSummary(
                case_id=c.id, name=c.name, n_actions=len(c.actions),
                n_assertions=len(c.assertions),
                static_issue_count=len(issues),
                has_errors=any(i.severity == "error" for i in issues),
            ))
        return ListCasesOutput(total=len(summaries), cases=summaries)

    return Tool(
        name="list_cases",
        description="列出待 review 的所有 testcase，附带静态校验问题数。",
        input_model=_NoInput, fn=_fn,
    )

def _build_inspect_case(ctx: CriticContext) -> Tool:
    def _fn(args: InspectCaseInput) -> InspectCaseOutput:
        case = next((c for c in ctx.plan.cases if c.id == args.case_id), None)
        if case is None:
            raise ValueError(f"未知 case_id={args.case_id!r}")
        issues = _ensure_issues(ctx, args.case_id)
        return InspectCaseOutput(
            case_id=case.id, name=case.name, description=case.description,
            actions=[a.model_dump() for a in case.actions],
            assertion_invariant_ids=[a.invariant.id for a in case.assertions],
            issues=[IssueDetail(**i.__dict__) for i in issues],
        )

    return Tool(
        name="inspect_case",
        description="查看某条 testcase 的完整动作、断言与静态问题。",
        input_model=InspectCaseInput, fn=_fn,
    )

def _build_patch_case(ctx: CriticContext) -> Tool:
    def _fn(args: PatchCaseInput) -> PatchCaseOutput:
        case = next((c for c in ctx.plan.cases if c.id == args.case_id), None)
        if case is None:
            return PatchCaseOutput(ok=False, case_id=args.case_id,
                                   error="case not found")
        # 把 dict actions 转回 Action 对象
        new_actions = []
        for a in args.new_actions:
            try:
                kind = a["kind"]
                if kind == "cast":
                    new_actions.append(CastAction(
                        actor=a["actor"], skill=a["skill"], target=a["target"]
                    ))
                elif kind == "wait":
                    new_actions.append(WaitAction(seconds=float(a["seconds"])))
                elif kind == "interrupt":
                    new_actions.append(InterruptAction(actor=a["actor"]))
                elif kind == "noop":
                    new_actions.append(NoopAction())
                else:
                    return PatchCaseOutput(
                        ok=False, case_id=args.case_id,
                        error=f"未知 action kind: {kind!r}",
                    )
            except (KeyError, TypeError, ValueError) as e:
                return PatchCaseOutput(
                    ok=False, case_id=args.case_id,
                    error=f"action 解析失败：{e}",
                )
        case.actions = new_actions
        # 重置缓存让下次 inspect 拿到新静态结果
        ctx.issues_cache.pop(case.id, None)
        ctx.patched[case.id] = args.rationale
        return PatchCaseOutput(ok=True, case_id=case.id)

    return Tool(
        name="patch_case",
        description=(
            "替换指定 case 的 actions（保留 id/name/assertions）。用于修复"
            "静态校验发现的 MP/CD/wait 时长问题。"
        ),
        input_model=PatchCaseInput, fn=_fn,
    )

def _build_drop_case(ctx: CriticContext) -> Tool:
    def _fn(args: DropCaseInput) -> DropCaseOutput:
        idx = next((i for i, c in enumerate(ctx.plan.cases) if c.id == args.case_id), -1)
        if idx == -1:
            return DropCaseOutput(ok=False, case_id=args.case_id,
                                  error="case not found")
        ctx.plan.cases.pop(idx)
        ctx.dropped.append(args.case_id)
        return DropCaseOutput(ok=True, case_id=args.case_id)

    return Tool(
        name="drop_case",
        description=(
            "彻底删除某条 case（无法救活的 case 用此工具）。"
            "如果 case 还能 patch 修好，优先 patch_case。"
        ),
        input_model=DropCaseInput, fn=_fn,
    )

def _build_accept_case(ctx: CriticContext) -> Tool:
    def _fn(args: AcceptCaseInput) -> dict:
        ctx.accepted.add(args.case_id)
        return {"ok": True, "case_id": args.case_id}

    return Tool(
        name="accept_case",
        description="对没问题的 case 显式 accept（可选；不调也不影响 plan）。",
        input_model=AcceptCaseInput, fn=_fn,
    )

def _build_finalize(ctx: CriticContext) -> Tool:
    def _fn(args: FinalizeInput) -> FinalizeOutput:
        ctx.finalized = True
        ctx.finalize_reason = args.reason
        return FinalizeOutput(
            accepted=len(ctx.accepted),
            patched=len(ctx.patched),
            dropped=len(ctx.dropped),
        )

    return Tool(
        name="finalize",
        description="所有 case review 完毕后调用以结束。",
        input_model=FinalizeInput, fn=_fn,
    )
