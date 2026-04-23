"""不变式 DSL。

一条 Invariant 是纯数据：DesignDocAgent 把它作为 JSON emit 出来，TestGenAgent
挂到测试用例上。实际判断交给一份按 kind 索引的 Python 函数注册表——这样 LLM
的工具表面很小，也不会跑到任意由 LLM 生成的代码。

每条 invariant 有一个 kind 鉴别器。kind 故意设计得窄，名字直接对应它要抓的
现实属性。加一条新属性 = 加一个新 kind + 一个 evaluator，大约 10 行代码，
和普通代码一样走 review。

D1-D2 期间支持的 kind：
    - hp_nonneg                         : ALWAYS character.hp >= 0
    - mp_nonneg                         : ALWAYS character.mp >= 0
    - cooldown_at_least_after_cast      : AFTER cast(skill): cooldown >= expected
    - buff_stacks_within_limit          : ALWAYS stack_count(buff) <= max_stacks
    - buff_refresh_magnitude_stable     : refresh buff 不能把 magnitude 撑大
    - interrupt_clears_casting          : AFTER interrupt: casting_skill is None
    - interrupt_refunds_mp              : AFTER interrupt: mp 被 mp_cost 退回来
    - replay_deterministic              : 同 seed 两次跑事件序列必须一致

后面 D8 / D17 又加了 DoT、QuestSim 相关的 10+ 条，见文件下方。

为什么用 registry 而不是自由 Python 或自写 parser：
  - LLM 安全：Agent 没办法往 invariant 里塞 exec
  - 类型安全：mypy 能抓 evaluator 签名错
  - 可观测：invariant 序列化成 JSON 后能原样进报告

Drools、OPA / Rego 这些生产规则引擎都是这个路子，保守但可靠。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Literal

from pydantic import BaseModel, Field

from gameguard.domain.event import EventLog

# --------------------------------------------------------------------------- #
# State view — what evaluators see
# --------------------------------------------------------------------------- #

@dataclass
class StateView:
    """A lightweight, read-only view of the sandbox state at a moment in time.

    We pass a StateView rather than the live sandbox so evaluators cannot
    mutate simulation state. ``get_character`` returns a *copy*.

    扩展字段（D17）：
      - `scene`：若 sandbox 有 Scene（QuestSim），evaluator 可读空间信息
      - `quest`：若 sandbox 有 Quest，evaluator 可读任务状态
      - `entities`：若 sandbox 有 EntityRegistry，evaluator 可读实体
    pysim 的 StateView 保持这些为 None（向后兼容）。
    """

    t: float
    tick: int
    characters: dict[str, Any]   # id -> Character (avoiding circular import)
    # D17 QuestSim 扩展字段（其它 sandbox 保持 None）
    scene: Any = None        # gameguard.domain.scene.Scene
    quest: Any = None        # gameguard.domain.quest.Quest
    entities: Any = None     # gameguard.domain.entity.EntityRegistry

    def get_character(self, char_id: str) -> Any:
        return self.characters[char_id]

# --------------------------------------------------------------------------- #
# Invariant specs (pure data, JSON-serializable)
# --------------------------------------------------------------------------- #

class _BaseInvariant(BaseModel):
    id: str
    description: str

class HpNonnegInvariant(_BaseInvariant):
    kind: Literal["hp_nonneg"] = "hp_nonneg"
    actor: str

class MpNonnegInvariant(_BaseInvariant):
    kind: Literal["mp_nonneg"] = "mp_nonneg"
    actor: str

class CooldownAtLeastAfterCastInvariant(_BaseInvariant):
    kind: Literal["cooldown_at_least_after_cast"] = "cooldown_at_least_after_cast"
    actor: str
    skill: str
    expected_cooldown: float
    tolerance: float = 0.05  # seconds

class BuffStacksWithinLimitInvariant(_BaseInvariant):
    kind: Literal["buff_stacks_within_limit"] = "buff_stacks_within_limit"
    actor: str
    buff: str
    max_stacks: int

class BuffRefreshMagnitudeStableInvariant(_BaseInvariant):
    kind: Literal["buff_refresh_magnitude_stable"] = "buff_refresh_magnitude_stable"
    actor: str
    buff: str
    expected_magnitude: float
    tolerance: float = 1e-6

class InterruptClearsCastingInvariant(_BaseInvariant):
    kind: Literal["interrupt_clears_casting"] = "interrupt_clears_casting"
    actor: str

class InterruptRefundsMpInvariant(_BaseInvariant):
    kind: Literal["interrupt_refunds_mp"] = "interrupt_refunds_mp"
    actor: str
    skill: str
    tolerance: float = 1e-6

class ReplayDeterministicInvariant(_BaseInvariant):
    kind: Literal["replay_deterministic"] = "replay_deterministic"
    # This one is meta — evaluated by replaying the same plan twice and
    # comparing traces. It has no state-view evaluator; see runner.

class DotTotalDamageWithinToleranceInvariant(_BaseInvariant):
    """I-09：DoT 总伤可预测（针对 buff_burn 等 is_dot=True 的 buff）。

    在 buff 的整个生命周期内累计 ``dot_tick`` 事件的 amount，应当
    ≈ ``magnitude * duration``。tolerance 默认 1.0（容许 1 个额外 tick
    的浮点边界——因为 4.0/0.05 在浮点下不严格相等于 80）。

    BUG-004 (v2 用 1.05 漂移系数) 会让 4s burn 的总伤从 40.5 漂到 42.5+，
    超出 tolerance 1.0 → 被抓到。
    """

    kind: Literal["dot_total_damage_within_tolerance"] = (
        "dot_total_damage_within_tolerance"
    )
    actor: str       # 被 DoT 的角色（target）
    buff: str        # 例 "buff_burn"
    expected_total: float
    tolerance: float = 1.0

# --- QuestSim invariant classes（D17，10 种） ---
class QuestStepReachableInvariant(_BaseInvariant):
    """I-Q1：quest end step 从 start_step 图可达（BFS）。"""

    kind: Literal["quest_step_reachable"] = "quest_step_reachable"
    quest_id: str

class QuestStepOnceInvariant(_BaseInvariant):
    """I-Q2：每个 step 至多 enter 一次（防重复触发）。"""

    kind: Literal["quest_step_once"] = "quest_step_once"
    quest_id: str

class QuestNoOrphanFlagInvariant(_BaseInvariant):
    """I-Q3：所有 set 的 flag 都被某 step/trigger 读取（无死 flag）。"""

    kind: Literal["quest_no_orphan_flag"] = "quest_no_orphan_flag"
    quest_id: str

class TriggerVolumeFiresOnEnterInvariant(_BaseInvariant):
    """I-Q4：玩家进入 volume 后同 tick / 下 tick 必发 trigger_fired。

    Q-BUG-001 的直接 oracle。
    """

    kind: Literal["trigger_volume_fires_on_enter"] = "trigger_volume_fires_on_enter"
    trigger_id: str

class NpcRespawnOnResetInvariant(_BaseInvariant):
    """I-Q5：quest_reset 事件后 npc 的 pos + state 必须恢复到 initial。

    Q-BUG-003 的直接 oracle。
    依赖 Entity.initial_pos / initial_state 被 `snapshot_initial` 记录过。
    """

    kind: Literal["npc_respawn_on_reset"] = "npc_respawn_on_reset"
    npc_id: str

class SaveLoadRoundTripInvariant(_BaseInvariant):
    """I-Q6：save → advance → load → state 恢复到 save 时。

    Q-BUG-004 的直接 oracle。 这是 meta-invariant，runner 特殊处理
    （类似 replay_deterministic）。
    """

    kind: Literal["save_load_round_trip"] = "save_load_round_trip"
    slot: str = "auto"

class PathExistsBetweenInvariant(_BaseInvariant):
    """I-Q7：NavGrid 上 from→to 存在 A* 路径。"""

    kind: Literal["path_exists_between"] = "path_exists_between"
    from_x: float
    from_y: float
    to_x: float
    to_y: float

class NoStuckPositionsInvariant(_BaseInvariant):
    """I-Q8：NavGrid 所有可走 cell 强连通（只有 1 个 component）。

    Q-BUG-005 的直接 oracle。
    """

    kind: Literal["no_stuck_positions"] = "no_stuck_positions"

class DialogueNoDeadBranchInvariant(_BaseInvariant):
    """I-Q9：DialogueGraph 每个 node 从 root 可达 + 每个非叶能到 terminal。"""

    kind: Literal["dialogue_no_dead_branch"] = "dialogue_no_dead_branch"
    dialogue_id: str

class InteractionRangeConsistentInvariant(_BaseInvariant):
    """I-Q10：每次 interact 事件，actor→entity 距离 ≤ entity.interact_range。"""

    kind: Literal["interaction_range_consistent"] = "interaction_range_consistent"

# ------ Invariant union（skill + quest 所有种类） ------

Invariant = (
    HpNonnegInvariant
    | MpNonnegInvariant
    | CooldownAtLeastAfterCastInvariant
    | BuffStacksWithinLimitInvariant
    | BuffRefreshMagnitudeStableInvariant
    | InterruptClearsCastingInvariant
    | InterruptRefundsMpInvariant
    | ReplayDeterministicInvariant
    | DotTotalDamageWithinToleranceInvariant
    | QuestStepReachableInvariant
    | QuestStepOnceInvariant
    | QuestNoOrphanFlagInvariant
    | TriggerVolumeFiresOnEnterInvariant
    | NpcRespawnOnResetInvariant
    | SaveLoadRoundTripInvariant
    | PathExistsBetweenInvariant
    | NoStuckPositionsInvariant
    | DialogueNoDeadBranchInvariant
    | InteractionRangeConsistentInvariant
)

class InvariantBundle(BaseModel):
    """A set of invariants emitted from a design doc."""

    items: list[Invariant] = Field(default_factory=list)

    def by_id(self, inv_id: str) -> Invariant:
        for inv in self.items:
            if inv.id == inv_id:
                return inv
        raise KeyError(inv_id)

# --------------------------------------------------------------------------- #
# Evaluation results
# --------------------------------------------------------------------------- #

class InvariantResult(BaseModel):
    invariant_id: str
    passed: bool
    message: str = ""
    witness_tick: int | None = None
    witness_t: float | None = None
    actual: Any = None
    expected: Any = None

# --------------------------------------------------------------------------- #
# Evaluator registry
# --------------------------------------------------------------------------- #

Evaluator = Callable[[Any, StateView, EventLog], InvariantResult]
_REGISTRY: dict[str, Evaluator] = {}

def register(kind: str) -> Callable[[Evaluator], Evaluator]:
    def deco(fn: Evaluator) -> Evaluator:
        _REGISTRY[kind] = fn
        return fn

    return deco

def evaluate(inv: Invariant, view: StateView, log: EventLog) -> InvariantResult:
    fn = _REGISTRY.get(inv.kind)
    if fn is None:
        return InvariantResult(
            invariant_id=inv.id,
            passed=False,
            message=f"no evaluator registered for kind={inv.kind!r}",
        )
    return fn(inv, view, log)

# --------------------------------------------------------------------------- #
# Built-in evaluators
# --------------------------------------------------------------------------- #

@register("hp_nonneg")
def _eval_hp_nonneg(inv: HpNonnegInvariant, view: StateView, log: EventLog) -> InvariantResult:
    c = view.get_character(inv.actor)
    if c.hp < 0:
        return InvariantResult(
            invariant_id=inv.id,
            passed=False,
            message=f"{inv.actor}.hp={c.hp} is negative",
            witness_tick=view.tick,
            witness_t=view.t,
            actual=c.hp,
            expected=">= 0",
        )
    return InvariantResult(invariant_id=inv.id, passed=True)

@register("mp_nonneg")
def _eval_mp_nonneg(inv: MpNonnegInvariant, view: StateView, log: EventLog) -> InvariantResult:
    c = view.get_character(inv.actor)
    if c.mp < -1e-6:
        return InvariantResult(
            invariant_id=inv.id,
            passed=False,
            message=f"{inv.actor}.mp={c.mp} is negative",
            witness_tick=view.tick,
            witness_t=view.t,
            actual=c.mp,
            expected=">= 0",
        )
    return InvariantResult(invariant_id=inv.id, passed=True)

@register("cooldown_at_least_after_cast")
def _eval_cd(
    inv: CooldownAtLeastAfterCastInvariant, view: StateView, log: EventLog
) -> InvariantResult:
    # Find the most recent cast_complete event for this actor/skill.
    casts = [
        e
        for e in log.events
        if e.kind == "cast_complete" and e.actor == inv.actor and e.skill == inv.skill
    ]
    if not casts:
        # No cast yet — vacuously true.
        return InvariantResult(invariant_id=inv.id, passed=True)
    last_cast_t = casts[-1].t
    elapsed = view.t - last_cast_t
    remaining = max(0.0, inv.expected_cooldown - elapsed)
    c = view.get_character(inv.actor)
    actual = c.cooldown_remaining(inv.skill)
    # Assert the actual remaining cooldown is within tolerance of expected.
    if abs(actual - remaining) > inv.tolerance:
        return InvariantResult(
            invariant_id=inv.id,
            passed=False,
            message=(
                f"{inv.actor}.{inv.skill} cooldown={actual:.3f}s, "
                f"expected ~{remaining:.3f}s after cast at t={last_cast_t:.3f}"
            ),
            witness_tick=view.tick,
            witness_t=view.t,
            actual=actual,
            expected=remaining,
        )
    return InvariantResult(invariant_id=inv.id, passed=True)

@register("buff_stacks_within_limit")
def _eval_stacks(
    inv: BuffStacksWithinLimitInvariant, view: StateView, log: EventLog
) -> InvariantResult:
    c = view.get_character(inv.actor)
    total = sum(b.stacks for b in c.buffs if b.spec_id == inv.buff)
    if total > inv.max_stacks:
        return InvariantResult(
            invariant_id=inv.id,
            passed=False,
            message=f"{inv.buff} stacks={total} exceeds max={inv.max_stacks}",
            witness_tick=view.tick,
            witness_t=view.t,
            actual=total,
            expected=inv.max_stacks,
        )
    return InvariantResult(invariant_id=inv.id, passed=True)

@register("buff_refresh_magnitude_stable")
def _eval_refresh(
    inv: BuffRefreshMagnitudeStableInvariant, view: StateView, log: EventLog
) -> InvariantResult:
    c = view.get_character(inv.actor)
    for b in c.buffs:
        if b.spec_id == inv.buff and abs(b.magnitude - inv.expected_magnitude) > inv.tolerance:
            return InvariantResult(
                invariant_id=inv.id,
                passed=False,
                message=(
                    f"{inv.buff} on {inv.actor}: magnitude={b.magnitude}, "
                    f"expected {inv.expected_magnitude}"
                ),
                witness_tick=view.tick,
                witness_t=view.t,
                actual=b.magnitude,
                expected=inv.expected_magnitude,
            )
    return InvariantResult(invariant_id=inv.id, passed=True)

@register("interrupt_clears_casting")
def _eval_interrupt_clears(
    inv: InterruptClearsCastingInvariant, view: StateView, log: EventLog
) -> InvariantResult:
    # Check only after the most recent interrupt event; if casting field
    # is still set, the interrupt handler didn't clean up.
    interrupts = [e for e in log.events if e.kind == "cast_interrupted" and e.actor == inv.actor]
    if not interrupts:
        return InvariantResult(invariant_id=inv.id, passed=True)
    c = view.get_character(inv.actor)
    if c.casting_skill is not None:
        return InvariantResult(
            invariant_id=inv.id,
            passed=False,
            message=f"{inv.actor} still casting {c.casting_skill!r} after interrupt",
            witness_tick=view.tick,
            witness_t=view.t,
            actual=c.casting_skill,
            expected=None,
        )
    return InvariantResult(invariant_id=inv.id, passed=True)

@register("dot_total_damage_within_tolerance")
def _eval_dot_total(
    inv: DotTotalDamageWithinToleranceInvariant,
    view: StateView,
    log: EventLog,
) -> InvariantResult:
    """累加所有针对 ``inv.actor`` 的 ``dot_tick`` 事件，验证总伤接近预期。

    遍历整个 EventLog 即可——dot_tick 事件由 sandbox 在每 tick 发射，
    带 ``target`` 和 ``buff`` 字段。
    """
    total = 0.0
    for e in log.events:
        if (
            e.kind == "dot_tick"
            and e.target == inv.actor
            and e.buff == inv.buff
            and e.amount is not None
        ):
            total += e.amount
    if abs(total - inv.expected_total) > inv.tolerance:
        return InvariantResult(
            invariant_id=inv.id,
            passed=False,
            message=(
                f"{inv.buff} on {inv.actor}: 总 DoT 伤害 {total:.4f}，"
                f"预期 {inv.expected_total} (tolerance {inv.tolerance})"
            ),
            actual=round(total, 4),
            expected=inv.expected_total,
        )
    return InvariantResult(invariant_id=inv.id, passed=True)

@register("interrupt_refunds_mp")
def _eval_interrupt_refund(
    inv: InterruptRefundsMpInvariant, view: StateView, log: EventLog
) -> InvariantResult:
    # The sandbox emits a dedicated meta field when refund happens; we
    # check the most recent interrupt event involving the skill.
    interrupts = [
        e
        for e in log.events
        if e.kind == "cast_interrupted"
        and e.actor == inv.actor
        and e.skill == inv.skill
    ]
    if not interrupts:
        return InvariantResult(invariant_id=inv.id, passed=True)
    last = interrupts[-1]
    refunded = last.meta.get("mp_refunded")
    if not refunded:
        return InvariantResult(
            invariant_id=inv.id,
            passed=False,
            message=(
                f"{inv.actor}.{inv.skill} interrupted at t={last.t:.3f} "
                f"but mp_refunded flag is {refunded!r}"
            ),
            witness_tick=last.tick,
            witness_t=last.t,
            actual=refunded,
            expected=True,
        )
    return InvariantResult(invariant_id=inv.id, passed=True)

# --- QuestSim evaluators（D17，9 种 + save_load 走 runner 后门） ---
@register("quest_step_reachable")
def _eval_quest_step_reachable(
    inv: QuestStepReachableInvariant, view: StateView, log: EventLog
) -> InvariantResult:
    """BFS quest graph，检查 end_step_ids 是否全在可达集。"""
    quest = view.quest
    if quest is None or quest.id != inv.quest_id:
        return InvariantResult(
            invariant_id=inv.id, passed=False,
            message=f"StateView 无 quest 或 quest_id 不匹配：期望 {inv.quest_id!r}",
        )
    reachable = quest.reachable_step_ids()
    unreachable_ends = [s for s in quest.end_step_ids if s not in reachable]
    if unreachable_ends:
        return InvariantResult(
            invariant_id=inv.id, passed=False,
            message=(
                f"quest {inv.quest_id!r} 的 end steps {unreachable_ends} 从 start 不可达；"
                f"可达集大小 {len(reachable)}"
            ),
            actual=list(reachable),
            expected=quest.end_step_ids,
        )
    return InvariantResult(invariant_id=inv.id, passed=True)

@register("quest_step_once")
def _eval_quest_step_once(
    inv: QuestStepOnceInvariant, view: StateView, log: EventLog
) -> InvariantResult:
    """统计 quest_step_entered 事件，按 step_id 分组，计数 > 1 即违规。"""
    from collections import Counter
    counts: Counter[str] = Counter()
    for e in log.events:
        if e.kind != "quest_step_entered":
            continue
        meta = e.meta or {}
        if meta.get("quest_id") != inv.quest_id:
            continue
        step_id = meta.get("step_id")
        if step_id:
            counts[step_id] += 1
    repeats = {sid: c for sid, c in counts.items() if c > 1}
    if repeats:
        return InvariantResult(
            invariant_id=inv.id, passed=False,
            message=f"quest {inv.quest_id!r} 中这些 step 被重复进入：{repeats}",
            actual=repeats,
        )
    return InvariantResult(invariant_id=inv.id, passed=True)

@register("quest_no_orphan_flag")
def _eval_quest_no_orphan_flag(
    inv: QuestNoOrphanFlagInvariant, view: StateView, log: EventLog
) -> InvariantResult:
    """静态分析：set 的 flag 是否都被读取（requires_flags 或 flag_true trigger）。"""
    quest = view.quest
    if quest is None or quest.id != inv.quest_id:
        return InvariantResult(
            invariant_id=inv.id, passed=False,
            message="StateView 无 quest 或 id 不匹配",
        )
    orphans = quest.orphan_flags()
    if orphans:
        return InvariantResult(
            invariant_id=inv.id, passed=False,
            message=f"quest {inv.quest_id!r} 有孤儿 flag（set 但无人读）：{sorted(orphans)}",
            actual=sorted(orphans),
        )
    return InvariantResult(invariant_id=inv.id, passed=True)

@register("trigger_volume_fires_on_enter")
def _eval_trigger_volume_fires(
    inv: TriggerVolumeFiresOnEnterInvariant, view: StateView, log: EventLog
) -> InvariantResult:
    """若 trace 中有实体进入 volume 的帧，后续必有 trigger_fired(trigger_id)。

    检测策略：找 trace 中该 trigger 的 trigger_fired 事件；如果整个 run 内
    玩家的 pos 曾进入 volume.bbox 但没 fire，判违反。
    """
    scene = view.scene
    if scene is None:
        return InvariantResult(
            invariant_id=inv.id, passed=False,
            message="StateView 无 scene，无法检查 trigger",
        )
    vol = scene.get_trigger(inv.trigger_id)
    if vol is None:
        return InvariantResult(
            invariant_id=inv.id, passed=False,
            message=f"scene 里没有 trigger {inv.trigger_id!r}",
        )

    # 若玩家结束时 pos 在 volume 内 → 必须 fired 过
    # （更完整的实现会遍历整个 trace 的 pos 历史；D17 简化到结束态）
    fired_events = [
        e for e in log.events
        if e.kind == "trigger_fired"
        and e.target == inv.trigger_id
        and (e.meta or {}).get("trigger_kind") == "enter_volume"
    ]

    # 检查是否有 watch entity 当前位于 volume 内
    currently_inside = False
    if view.entities is not None:
        for eid in vol.watch_entity_ids:
            e = view.entities.get_optional(eid)
            if e is not None and vol.bbox.contains_point(e.pos, inclusive=True):
                currently_inside = True
                break

    if currently_inside and not fired_events:
        return InvariantResult(
            invariant_id=inv.id, passed=False,
            message=(
                f"watch entity 当前在 volume {inv.trigger_id!r} 内，但整个 run 未发 trigger_fired"
            ),
            actual="no fire event",
            expected="at least one trigger_fired",
        )
    return InvariantResult(invariant_id=inv.id, passed=True)

@register("npc_respawn_on_reset")
def _eval_npc_respawn_on_reset(
    inv: NpcRespawnOnResetInvariant, view: StateView, log: EventLog
) -> InvariantResult:
    """quest_reset 之后 npc.pos == initial_pos 且 npc.state == initial_state。"""
    if view.entities is None:
        return InvariantResult(
            invariant_id=inv.id, passed=False,
            message="StateView 无 entities",
        )
    npc = view.entities.get_optional(inv.npc_id)
    if npc is None:
        return InvariantResult(
            invariant_id=inv.id, passed=False,
            message=f"没有 npc id {inv.npc_id!r}",
        )
    # 只有在发生过 quest_reset 事件后才检查
    resets = [e for e in log.events if e.kind == "quest_reset"]
    if not resets:
        return InvariantResult(invariant_id=inv.id, passed=True)

    if npc.initial_pos is not None and npc.pos != npc.initial_pos:
        return InvariantResult(
            invariant_id=inv.id, passed=False,
            message=(
                f"{inv.npc_id}.pos={npc.pos} 未恢复到 initial {npc.initial_pos}"
            ),
            actual=str(npc.pos), expected=str(npc.initial_pos),
        )
    if npc.initial_state != npc.state:
        return InvariantResult(
            invariant_id=inv.id, passed=False,
            message=(
                f"{inv.npc_id}.state={npc.state} 未恢复到 initial {npc.initial_state}"
            ),
            actual=str(npc.state), expected=str(npc.initial_state),
        )
    return InvariantResult(invariant_id=inv.id, passed=True)

@register("path_exists_between")
def _eval_path_exists(
    inv: PathExistsBetweenInvariant, view: StateView, log: EventLog
) -> InvariantResult:
    """A* 查 from→to 路径。"""
    scene = view.scene
    if scene is None or scene.nav is None:
        return InvariantResult(
            invariant_id=inv.id, passed=False, message="无 scene.nav"
        )
    from gameguard.domain.geom import Vec3 as _Vec3
    from gameguard.sandbox.questsim.nav import astar
    start = scene.nav.world_to_grid(_Vec3(x=inv.from_x, y=inv.from_y, z=0))
    goal = scene.nav.world_to_grid(_Vec3(x=inv.to_x, y=inv.to_y, z=0))
    path = astar(scene.nav, start, goal)
    if path is None:
        return InvariantResult(
            invariant_id=inv.id, passed=False,
            message=f"A* 未找到从 ({inv.from_x},{inv.from_y}) 到 ({inv.to_x},{inv.to_y}) 的路径",
        )
    return InvariantResult(invariant_id=inv.id, passed=True)

@register("no_stuck_positions")
def _eval_no_stuck_positions(
    inv: NoStuckPositionsInvariant, view: StateView, log: EventLog
) -> InvariantResult:
    """NavGrid walkable cells 必须只有 1 个强连通分量。"""
    scene = view.scene
    if scene is None or scene.nav is None:
        return InvariantResult(
            invariant_id=inv.id, passed=False, message="无 scene.nav"
        )
    from gameguard.sandbox.questsim.nav import walkable_components
    comps = walkable_components(scene.nav)
    if len(comps) > 1:
        sizes = sorted((len(c) for c in comps), reverse=True)
        return InvariantResult(
            invariant_id=inv.id, passed=False,
            message=f"NavGrid 有 {len(comps)} 个不连通的可走区域；各分量大小 {sizes}",
            actual=len(comps), expected=1,
        )
    return InvariantResult(invariant_id=inv.id, passed=True)

@register("dialogue_no_dead_branch")
def _eval_dialogue_no_dead_branch(
    inv: DialogueNoDeadBranchInvariant, view: StateView, log: EventLog
) -> InvariantResult:
    """DialogueGraph 每个 node 从 root 可达，且能到 terminal。"""
    # DialogueGraph 放在 sandbox config.dialogues；通过 scene 间接关联。
    # 这里我们只能从 view.quest 或 config 取；简化：若 StateView 无 dialogue，返回 skip-pass
    # 完整实现需要把 dialogues dict 一起传进 StateView；D17 先支持 scene 无 dialog 时的静态
    # 分析（从外部传入 dialogue_graph 到 evaluator 的 kwargs，由 runner/caller 管）。
    dialogues = getattr(view, "dialogues", None)
    if not dialogues:
        return InvariantResult(
            invariant_id=inv.id, passed=False,
            message="StateView 未关联任何 dialogue graph",
        )
    # dialogues 字典是 npc_id → DialogueGraph；我们按 graph.id 匹配
    graph = None
    for g in dialogues.values():
        if g.id == inv.dialogue_id:
            graph = g
            break
    if graph is None:
        return InvariantResult(
            invariant_id=inv.id, passed=False,
            message=f"找不到 dialogue_id={inv.dialogue_id!r}",
        )
    reach = graph.reachable_from_root()
    orphans = set(graph.all_node_ids()) - reach
    can_terminal = graph.nodes_that_can_reach_terminal()
    dead_ends = (
        set(graph.all_node_ids()) - can_terminal - {n.id for n in graph.nodes.values() if n.is_terminal()}
    )
    if orphans or dead_ends:
        return InvariantResult(
            invariant_id=inv.id, passed=False,
            message=(
                f"dialogue {inv.dialogue_id!r} 有孤儿节点 {sorted(orphans)} 或死胡同 {sorted(dead_ends)}"
            ),
            actual={"orphans": sorted(orphans), "dead_ends": sorted(dead_ends)},
        )
    return InvariantResult(invariant_id=inv.id, passed=True)

@register("interaction_range_consistent")
def _eval_interaction_range(
    inv: InteractionRangeConsistentInvariant, view: StateView, log: EventLog
) -> InvariantResult:
    """每次 interact 事件的 meta.distance 必须 ≤ entity.interact_range。"""
    if view.entities is None:
        return InvariantResult(
            invariant_id=inv.id, passed=False, message="无 entities"
        )
    for e in log.events:
        if e.kind != "trigger_fired":
            continue
        meta = e.meta or {}
        if meta.get("trigger_kind") != "interact_entity":
            continue
        ent = view.entities.get_optional(e.target)
        if ent is None:
            continue
        dist = meta.get("distance")
        if dist is None:
            continue
        if dist > ent.interact_range + 1e-6:
            return InvariantResult(
                invariant_id=inv.id, passed=False,
                message=(
                    f"interact on {e.target!r} at t={e.t}: 距离 {dist} > "
                    f"interact_range {ent.interact_range}"
                ),
                witness_t=e.t, witness_tick=e.tick,
                actual=dist, expected=ent.interact_range,
            )
    return InvariantResult(invariant_id=inv.id, passed=True)
