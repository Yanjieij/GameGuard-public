"""TestGen 工具集 —— TestGenAgent 调用的 tool。

职责边界

TestGenAgent 的输入是：
  - DesignDocAgent 产出的 ``InvariantBundle``
  - 沙箱的元数据（能看到有哪些技能 / 角色 / 资源）

输出：一份 ``TestPlan``。

所以本文件定义的 tool 对应这三类操作：

  1) read-only 查询：
       - list_invariants    了解要覆盖什么
       - list_skills        了解能用什么技能、参数是什么
       - list_characters    了解主角/敌方的初始属性
  2) emit（side-channel 输出）：
       - emit_testcase      把一条用例保存下来

  3) finalize：
       - finalize           宣告工作结束（和 DesignDocAgent 一致的收尾约定）

设计上的细节：
  - LLM 在 emit_testcase 时只传 invariant 的 id，工具内部帮它去
    InvariantBundle 里取。这避免让 LLM 重复抄写完整的 invariant 结构
    （又慢又容易抄错）。
  - actions 仍由 LLM 自己写，但我们提供了 action kind 的枚举描述让它
    不用猜。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel, Field

from gameguard.domain.action import Action, CastAction, InterruptAction, NoopAction, WaitAction
from gameguard.domain.character import Character
from gameguard.domain.invariant import InvariantBundle
from gameguard.domain.skill import SkillBook
from gameguard.testcase.model import (
    Assertion,
    AssertionWhen,
    TestCase,
    TestPlan,
    TestStrategy,
)
from gameguard.tools.schemas import Tool

# --- 查询类 tool 的 I/O schema ---
class _NoInput(BaseModel):
    pass

class InvariantSummary(BaseModel):
    """一条不变式的"简报"（LLM 视角）。"""

    id: str
    kind: str
    description: str
    params: dict[str, Any] = Field(default_factory=dict)

class InvariantListOutput(BaseModel):
    invariants: list[InvariantSummary]

class SkillSummary(BaseModel):
    id: str
    name: str
    mp_cost: float
    cast_time: float
    cooldown: float
    damage_base: float
    damage_type: str
    self_buffs: list[str]
    target_buffs: list[str]

class SkillListOutput(BaseModel):
    skills: list[SkillSummary]

class CharacterSummary(BaseModel):
    id: str
    name: str
    hp: float
    mp: float

class CharacterListOutput(BaseModel):
    characters: list[CharacterSummary]

# --- emit_testcase 的 schema ---
class ActionInput(BaseModel):
    """LLM 能填的动作字段。

    用一个扁平的可选字段模型（而不是 discriminated union）简化 LLM 的
    输出。我们自己在执行前做 ``_to_action`` 转换。
    """

    kind: str = Field(
        ...,
        description=(
            "动作类型。允许值："
            "'cast'(actor, skill, target), "
            "'wait'(seconds), "
            "'interrupt'(actor), "
            "'noop'()"
        ),
    )
    actor: str | None = None
    skill: str | None = None
    target: str | None = None
    seconds: float | None = None

class EmitTestCaseInput(BaseModel):
    """``emit_testcase`` 参数。

    我们要求 LLM 尽量引用 invariant_id，而不是再抄一遍 invariant
    的完整 JSON；引用失败（id 不存在）会被 tool 拒绝并回传错误消息。
    """

    id: str = Field(..., description="测试用例稳定 ID（kebab-case 推荐，如 'cooldown-fireball-after-switch'）")
    name: str = Field(..., description="用例的中文短描述，进报告标题")
    description: str = Field("", description="长描述，面试讲故事 / bug 报告引用")
    tags: list[str] = Field(
        default_factory=list, description="例：['skill', 'cooldown', 'regression']"
    )
    derived_from: list[str] = Field(
        default_factory=list,
        description="对照条款，建议写 'invariant:<id>' 或 'doc:<section heading>'",
    )
    seed: int = Field(42, description="沙箱 RNG seed；回归用例里同一条 seed 固定")
    sandbox: str = Field("pysim:v1", description="目标沙箱，通常固定 'pysim:v1'")
    actions: list[ActionInput] = Field(
        ...,
        description="按序执行的动作列表。至少要有一条 action 否则用例无意义。",
    )
    assertion_invariant_ids: list[str] = Field(
        ...,
        description=(
            "你要在这条用例末尾检查的 Invariant id 列表。必须是 "
            "list_invariants 里出现过的 id。系统会按 end_of_run 时机绑定。"
        ),
    )

class FinalizeInput(BaseModel):
    reason: str = "done"

class EmitResult(BaseModel):
    ok: bool
    case_id: str
    count_so_far: int
    error: str | None = None

class FinalizeResult(BaseModel):
    ok: bool = True
    count: int = 0
    message: str = "ok"

# --- 运行期上下文 ---
@dataclass
class TestGenContext:
    """给 TestGenAgent 看的世界：bundle + skill_book + 角色初始属性。"""

    bundle: InvariantBundle
    skill_book: SkillBook
    initial_characters: list[Character]
    # 运行期结果收集
    cases: list[TestCase] = field(default_factory=list)
    finalized: bool = False
    finalize_reason: str = ""

# --- 工具工厂 ---
def build_testgen_tools(ctx: TestGenContext) -> list[Tool]:
    """把 TestGenContext 包装成 LLM 能用的 Tool 列表。"""
    return [
        _build_list_invariants(ctx),
        _build_list_skills(ctx),
        _build_list_characters(ctx),
        _build_emit_testcase(ctx),
        _build_finalize(ctx),
    ]

# ---- list_invariants ------------------------------------------------------- #

def _build_list_invariants(ctx: TestGenContext) -> Tool:
    def _fn(_: _NoInput) -> InvariantListOutput:
        summaries: list[InvariantSummary] = []
        for inv in ctx.bundle.items:
            d = inv.model_dump()
            # params = 除了 id/kind/description 之外的字段
            params = {k: v for k, v in d.items() if k not in ("id", "kind", "description")}
            summaries.append(
                InvariantSummary(
                    id=inv.id, kind=inv.kind, description=inv.description, params=params
                )
            )
        return InvariantListOutput(invariants=summaries)

    return Tool(
        name="list_invariants",
        description=(
            "列出当前 DesignDocAgent 抽出来的所有不变式，供你设计测试用例时参考。"
            "在设计用例前先调用一次。"
        ),
        input_model=_NoInput,
        fn=_fn,
    )

# ---- list_skills ----------------------------------------------------------- #

def _build_list_skills(ctx: TestGenContext) -> Tool:
    def _fn(_: _NoInput) -> SkillListOutput:
        out: list[SkillSummary] = []
        for sid, spec in ctx.skill_book.specs.items():
            out.append(
                SkillSummary(
                    id=sid,
                    name=spec.name,
                    mp_cost=spec.mp_cost,
                    cast_time=spec.cast_time,
                    cooldown=spec.cooldown,
                    damage_base=spec.damage_base,
                    damage_type=spec.damage_type.value,
                    self_buffs=list(spec.self_buffs),
                    target_buffs=list(spec.target_buffs),
                )
            )
        return SkillListOutput(skills=out)

    return Tool(
        name="list_skills",
        description=(
            "列出沙箱中所有可释放的技能及其参数（mp/cast_time/cooldown/buffs）。"
            "设计动作序列时用来判断施法时机、等待多久、MP 是否够。"
        ),
        input_model=_NoInput,
        fn=_fn,
    )

# ---- list_characters ------------------------------------------------------- #

def _build_list_characters(ctx: TestGenContext) -> Tool:
    def _fn(_: _NoInput) -> CharacterListOutput:
        return CharacterListOutput(
            characters=[
                CharacterSummary(id=c.id, name=c.name, hp=c.hp, mp=c.mp)
                for c in ctx.initial_characters
            ]
        )

    return Tool(
        name="list_characters",
        description=(
            "列出沙箱初始的角色名单（id/name/hp/mp）。用例里的 actor / target "
            "字段必须是此列表里的 id。"
        ),
        input_model=_NoInput,
        fn=_fn,
    )

# ---- emit_testcase --------------------------------------------------------- #

def _build_emit_testcase(ctx: TestGenContext) -> Tool:
    inv_lookup = {inv.id: inv for inv in ctx.bundle.items}

    def _fn(args: EmitTestCaseInput) -> EmitResult:
        # 1) 校验 invariant 引用
        bad = [i for i in args.assertion_invariant_ids if i not in inv_lookup]
        if bad:
            return EmitResult(
                ok=False,
                case_id=args.id,
                count_so_far=len(ctx.cases),
                error=(
                    f"以下 invariant_id 不存在：{bad}。"
                    f"请先用 list_invariants 查看可用 id 列表。"
                ),
            )

        # 2) 动作 -> Action 子类型
        try:
            actions: list[Action] = [_to_action(a) for a in args.actions]
        except ValueError as e:
            return EmitResult(
                ok=False, case_id=args.id, count_so_far=len(ctx.cases), error=str(e)
            )

        if not actions:
            return EmitResult(
                ok=False,
                case_id=args.id,
                count_so_far=len(ctx.cases),
                error="用例必须至少有一条 action。",
            )

        # 3) 绑定断言
        assertions = [
            Assertion(invariant=inv_lookup[iid], when=AssertionWhen.END_OF_RUN)
            for iid in args.assertion_invariant_ids
        ]

        # 4) 保存
        case = TestCase(
            id=args.id,
            name=args.name,
            description=args.description,
            tags=args.tags,
            strategy=TestStrategy.CONTRACT,  # D5 contract 策略
            derived_from=args.derived_from,
            seed=args.seed,
            sandbox=args.sandbox,
            actions=actions,
            assertions=assertions,
        )
        # 去重（按 id）
        existing = {c.id: idx for idx, c in enumerate(ctx.cases)}
        if case.id in existing:
            ctx.cases[existing[case.id]] = case
        else:
            ctx.cases.append(case)
        return EmitResult(ok=True, case_id=case.id, count_so_far=len(ctx.cases))

    return Tool(
        name="emit_testcase",
        description=(
            "保存一条新的测试用例。一次调用对应一条 TestCase。记得在所有需要的"
            "用例 emit 完之后再 finalize。"
        ),
        input_model=EmitTestCaseInput,
        fn=_fn,
    )

# ---- finalize -------------------------------------------------------------- #

def _build_finalize(ctx: TestGenContext) -> Tool:
    def _fn(args: FinalizeInput) -> FinalizeResult:
        ctx.finalized = True
        ctx.finalize_reason = args.reason
        return FinalizeResult(count=len(ctx.cases), message=args.reason or "ok")

    return Tool(
        name="finalize",
        description=(
            "当你确认所有 invariants 都至少被一条 testcase 覆盖时，调用此工具结束。"
            "如果仍有 invariant 没有对应 testcase，不要调用此工具，继续 emit。"
        ),
        input_model=FinalizeInput,
        fn=_fn,
    )

# --- 动作转换 ---
def _to_action(a: ActionInput) -> Action:
    """把扁平的 ActionInput 转成 Action discriminated union 的某个子类型。"""
    kind = a.kind.lower()
    if kind == "cast":
        if not (a.actor and a.skill and a.target):
            raise ValueError("cast 动作需要 actor / skill / target 三个字段。")
        return CastAction(actor=a.actor, skill=a.skill, target=a.target)
    if kind == "wait":
        if a.seconds is None:
            raise ValueError("wait 动作需要 seconds。")
        if a.seconds <= 0:
            raise ValueError("wait.seconds 必须大于 0。")
        return WaitAction(seconds=a.seconds)
    if kind == "interrupt":
        if not a.actor:
            raise ValueError("interrupt 动作需要 actor。")
        return InterruptAction(actor=a.actor)
    if kind == "noop":
        return NoopAction()
    raise ValueError(f"不支持的 action kind: {kind!r}")

# --- 便捷入口：从 Context 打包成 TestPlan ---
def context_to_plan(
    ctx: TestGenContext, *, plan_id: str, plan_name: str, description: str = "", version: str = "0.1.0"
) -> TestPlan:
    return TestPlan(
        id=plan_id,
        name=plan_name,
        description=description,
        version=version,
        cases=list(ctx.cases),
    )
