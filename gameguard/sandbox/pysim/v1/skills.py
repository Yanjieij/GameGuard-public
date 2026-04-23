"""v1 技能 handler——黄金参考实现。

v2 的正确性以这份为基准。这里每个行为都和 docs/example_skill_v1.md 的设计
文档严格一致。给 v2 加 bug 时，不要同步修这里——v1 要始终保持正确。

涉及的 4 个技能：
    - skill_fireball：短施法，纯伤害，冷却正常
    - skill_frostbolt：给目标挂 buff_chilled（refresh 语义）
    - skill_ignite：瞬发，给目标挂 DoT buff_burn
    - skill_focus：长施法，给自己挂 buff_arcane_power（refresh）

所有伤害都会从 sandbox RNG 抽一次决定是否暴击。
"""
from __future__ import annotations

from gameguard.domain import (
    BuffBook,
    BuffInstance,
    BuffSpec,
    Character,
    CharacterState,
    DamageType,
    SkillBook,
    SkillSpec,
    StackRule,
)
from gameguard.sandbox.pysim.core import PySim, SkillHandler

CRIT_CHANCE = 0.2
CRIT_MULT = 1.5

def build_skill_book() -> SkillBook:
    book = SkillBook()
    book.register(
        SkillSpec(
            id="skill_fireball",
            name="Fireball",
            mp_cost=30.0,
            cast_time=1.0,
            cooldown=8.0,
            damage_base=50.0,
            damage_type=DamageType.FIRE,
            damage_formula="base",
        )
    )
    book.register(
        SkillSpec(
            id="skill_frostbolt",
            name="Frostbolt",
            mp_cost=25.0,
            cast_time=1.5,
            cooldown=6.0,
            damage_base=40.0,
            damage_type=DamageType.FROST,
            damage_formula="base",
            target_buffs=["buff_chilled"],
        )
    )
    book.register(
        SkillSpec(
            id="skill_ignite",
            name="Ignite",
            mp_cost=40.0,
            cast_time=0.0,
            cooldown=12.0,
            damage_base=0.0,
            damage_type=DamageType.FIRE,
            damage_formula="base",
            target_buffs=["buff_burn"],
        )
    )
    book.register(
        SkillSpec(
            id="skill_focus",
            name="Arcane Focus",
            mp_cost=20.0,
            cast_time=2.0,
            cooldown=20.0,
            damage_base=0.0,
            damage_formula="base",
            self_buffs=["buff_arcane_power"],
        )
    )
    return book

def build_buff_book() -> BuffBook:
    book = BuffBook()
    book.register(
        BuffSpec(
            id="buff_chilled",
            name="Chilled",
            magnitude=0.3,          # 减速 30%
            # duration 8s 略大于 frostbolt 冷却 6s，这样 "再次施放 frostbolt
            # 会触发 refresh"，给 BUG-002 (buff_refresh_magnitude_stable)
            # 提供可触发的窗口；否则 refresh 永远不会发生。
            duration=8.0,
            stack_rule=StackRule.REFRESH,
            max_stacks=1,
            is_debuff=True,
        )
    )
    book.register(
        BuffSpec(
            id="buff_burn",
            name="Burn",
            magnitude=10.0,         # 每秒 10 点伤害
            duration=4.0,
            stack_rule=StackRule.REFRESH,
            max_stacks=1,
            is_debuff=True,
            is_dot=True,            # 触发 sandbox 的 DoT-on-tick 结算
        )
    )
    book.register(
        BuffSpec(
            id="buff_arcane_power",
            name="Arcane Power",
            magnitude=0.2,          # 伤害 +20%
            duration=10.0,
            stack_rule=StackRule.REFRESH,
            max_stacks=1,
        )
    )
    return book

class V1SkillHandler(SkillHandler):
    """正确实现。v2 会改掉这里面的若干处。"""

    # --- 施法生命周期 -----------------------------------------------------

    def on_cast_start(
        self, sim: PySim, actor: Character, skill_id: str, target_id: str
    ) -> bool:
        spec = sim.skills.get(skill_id)
        actor.mp -= spec.mp_cost
        actor.state = CharacterState.CASTING
        actor.casting_skill = skill_id
        # cast_time = 0 表示下一个 tick 就完成施法。
        actor.cast_remaining = max(spec.cast_time, sim.tick_dt)
        return True

    def on_cast_complete(
        self, sim: PySim, actor: Character, skill_id: str, target_id: str
    ) -> None:
        spec = sim.skills.get(skill_id)
        # 冷却在 complete 时开始（不是 cast_start），这是设计文档里明确的约定。
        actor.cooldowns[skill_id] = spec.cooldown
        actor.state = CharacterState.IDLE
        actor.casting_skill = None
        actor.cast_remaining = 0.0

        sim.emit(
            kind="cooldown_started",
            actor=actor.id,
            skill=skill_id,
            amount=spec.cooldown,
        )
        sim.emit(kind="cast_complete", actor=actor.id, skill=skill_id, target=target_id)

        # 结算伤害（如果这个技能有伤害）。
        if spec.damage_base > 0:
            target = sim.state().characters.get(target_id, actor)
            crit = sim.rng.random() < CRIT_CHANCE
            dmg = spec.damage_base * (CRIT_MULT if crit else 1.0)
            # 施法者身上有 Arcane Power 就吃加成。
            for b in actor.buffs:
                if b.spec_id == "buff_arcane_power":
                    dmg *= 1.0 + b.magnitude
                    break
            target.hp = max(0.0, target.hp - dmg)
            sim.emit(
                kind="damage_dealt",
                actor=actor.id,
                target=target_id,
                skill=skill_id,
                amount=round(dmg, 6),
                meta={"crit": crit},
            )
            if target.hp <= 0:
                target.state = CharacterState.DEAD
                sim.emit(kind="death", actor=target_id)

        # 自己挂 buff。
        for buff_id in spec.self_buffs:
            _apply_buff(sim, actor, actor, buff_id)
        # 目标挂 buff。
        for buff_id in spec.target_buffs:
            target = sim.state().characters.get(target_id, actor)
            _apply_buff(sim, actor, target, buff_id)

    def on_interrupt(self, sim: PySim, actor: Character, skill_id: str) -> None:
        spec = sim.skills.get(skill_id)
        # v1 黄金实现：退还 mp、清施法状态、不惩罚冷却。
        actor.mp += spec.mp_cost
        actor.state = CharacterState.IDLE
        actor.casting_skill = None
        actor.cast_remaining = 0.0
        sim.emit(
            kind="cast_interrupted",
            actor=actor.id,
            skill=skill_id,
            meta={"mp_refunded": True},
        )

    # DoT-on-tick (D8)

    def on_dot_tick(
        self,
        sim: PySim,
        target: Character,
        buff_spec_id: str,
        magnitude: float,
        dt: float,
    ) -> None:
        """v1 黄金实现：每 tick 精确扣 magnitude * dt 血。

        burn 在 4.0s duration 下总伤 = magnitude * duration = 40（I-09 的 oracle
        期望值），和 tick_dt 无关。
        """
        damage = magnitude * dt
        target.hp = max(0.0, target.hp - damage)
        sim.emit(
            kind="dot_tick",
            target=target.id,
            buff=buff_spec_id,
            amount=round(damage, 6),
        )
        if target.hp <= 0:
            target.state = CharacterState.DEAD
            sim.emit(kind="death", actor=target.id)

# --------------------------------------------------------------------------- #
# Shared buff-application helper (used by v1; v2 may override).
# --------------------------------------------------------------------------- #

def _apply_buff(sim: PySim, source: Character, target: Character, buff_id: str) -> None:
    """按 spec 的 stack_rule 把 buff_id 挂到 target 身上。

    v1 正确行为：
        - REFRESH：duration 重置，magnitude 保持 spec 的值
        - REPLACE：duration 和 magnitude 都换成 spec 的值
        - ADD：stacks 累加到 max_stacks 上限，duration 重置
        - INDEPENDENT：追加一个新 BuffInstance
    """
    spec = sim.buffs.get(buff_id)
    # 找已经挂着的同 spec 实例。
    existing = next((b for b in target.buffs if b.spec_id == buff_id), None)

    if existing is not None:
        if spec.stack_rule == StackRule.REFRESH:
            existing.remaining = spec.duration
            existing.magnitude = spec.magnitude   # 必须保持稳定，这是 BUG-002 的 oracle
            sim.emit(
                kind="buff_refreshed",
                actor=target.id,
                buff=buff_id,
                amount=existing.magnitude,
            )
            return
        if spec.stack_rule == StackRule.REPLACE:
            existing.remaining = spec.duration
            existing.magnitude = spec.magnitude
            existing.stacks = 1
            sim.emit(kind="buff_applied", actor=target.id, buff=buff_id, amount=spec.magnitude)
            return
        if spec.stack_rule == StackRule.ADD:
            if existing.stacks < spec.max_stacks:
                existing.stacks += 1
            existing.remaining = spec.duration
            sim.emit(
                kind="buff_applied",
                actor=target.id,
                buff=buff_id,
                amount=spec.magnitude,
                meta={"stacks": existing.stacks},
            )
            return
        if spec.stack_rule == StackRule.INDEPENDENT:
            pass  # 落到下面的 append

    target.buffs.append(
        BuffInstance(
            spec_id=buff_id,
            magnitude=spec.magnitude,
            remaining=spec.duration,
            stacks=1,
            source_id=source.id,
            applied_at=sim.state().t,
        )
    )
    sim.emit(kind="buff_applied", actor=target.id, buff=buff_id, amount=spec.magnitude)
