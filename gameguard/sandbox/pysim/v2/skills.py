"""v2 技能实现 —— 故意植入 5 个 bug 的版本。

本文件的角色

GameGuard 的整个故事线就是 "v1 跑绿、v2 跑红"。v2 必须保留与 v1 一模一
样的接口、数据表、技能列表、buff 列表，只在内部行为上埋几枚雷，
让 GameGuard 的 Agent 体系能在不知道 v2 改动细节的情况下，单凭策划文档
推出来的不变式抓住违规。

这份 "故意制造 bug" 的设计参考 NetEase Wuji（ASE 2019）对 1349 个商业
游戏 bug 的实证分类——我们挑了其中最有代表性的 5 类：

  - BUG-001  cooldown 错误重置          ← 状态污染
  - BUG-002  buff refresh 累加 magnitude ← 数值溢出
  - BUG-003  打断后状态机污染            ← 状态机死锁/泄漏
  - BUG-004  DoT 浮点累积误差            ← 数值精度
  - BUG-005  暴击 RNG 不走全局 seed      ← 确定性破坏

每个 bug 都用一个清晰的注释块圈出来，并标注它会被哪条不变式抓到。
这样面试时翻 v2/skills.py 就能"指着代码讲故事"。

重要约束：v2 必须能"骗过"一部分用例

如果 v2 100% 都是错的，那是 "完全不同的实现"，没有 demo 价值。
v2 的设计目标是：大多数行为正确，只在少数边缘场景里表现出 bug。
这才是真实游戏迭代里 regression 的常态——一个 PR 改了一处，碰巧破坏了
看似不相关的另一处。

每个 bug 的触发条件都很具体：
  - BUG-001 仅在"切换技能"瞬间触发
  - BUG-002 仅在"refresh 同名 buff"时累加
  - BUG-003 仅在"施法被打断"时触发
  - BUG-004 仅在 DoT tick 数较多时显著
  - BUG-005 仅在"重放对比"时显形

因此普通的施法/伤害/buff 应用走的还是 v1 的正确路径——v2 会复用 v1 的
绝大多数代码，只覆写 SkillHandler 中 bug 涉及的那几个方法。
"""
from __future__ import annotations

import random

from gameguard.domain import (
    Character,
    CharacterState,
    StackRule,
)
from gameguard.sandbox.pysim.core import PySim
from gameguard.sandbox.pysim.v1.skills import (
    CRIT_CHANCE,
    CRIT_MULT,
    V1SkillHandler,
    _apply_buff as _v1_apply_buff,
    build_buff_book as _build_buff_book_v1,
    build_skill_book as _build_skill_book_v1,
)

# v2 BUG-004 的"漂移系数"：v1 应该是 1.0（精确）。
# v2 用 1.05 模拟"程序员加了一段错误的补偿系数"型 regression。
# 4s burn 在 80 ticks 下 v1 总伤 = 40，v2 总伤 = 42 → 超出 0.5 容忍。
DOT_BUG_DRIFT_MULTIPLIER = 1.05

# v2 的数据表与 v1 完全一致 —— 只覆写行为。这模拟 "策划没改表，程序员
# 改了实现" 这种最常见的 regression 引入路径。
build_skill_book = _build_skill_book_v1
build_buff_book = _build_buff_book_v1

class V2SkillHandler(V1SkillHandler):
    """v2 的 SkillHandler。继承 v1，只覆写有 bug 的方法。

    这种"继承 + 覆写"的写法对面试讲故事很有利：你能清楚地指出每个
    bug 是 v1 的某个方法被错误修改而引入的——和真实代码 review 中的
    "diff" 视角完全一致。
    """

    # BUG-001 · cooldown 错误重置

    # 触发条件：施法者 A 技能在冷却期间，再施放 B 技能。v2 的 on_cast_start
    #          错误地"清空所有冷却"——实现者本意可能是"重置当前技能的
    #          冷却"，结果写错了循环条件。

    # 抓得到的不变式：
    #   I-04-fireball / I-04-frostbolt / ...  cooldown_at_least_after_cast

    def on_cast_start(
        self, sim: PySim, actor: Character, skill_id: str, target_id: str
    ) -> bool:
        spec = sim.skills.get(skill_id)
        actor.mp -= spec.mp_cost
        actor.state = CharacterState.CASTING
        actor.casting_skill = skill_id
        actor.cast_remaining = max(spec.cast_time, sim.tick_dt)

        # ---- BUG-001：错误地清空所有冷却 ----
        # 实现者本意：开始新施法前清理临时冷却标记
        # 实际行为：把所有技能的冷却都清零（包括正在冷却中的）
        # 这是真实项目里"复制粘贴 + 改错变量名"造成的典型 regression
        actor.cooldowns.clear()

        return True

    # BUG-003 · 打断后状态机污染（mp 未退款 + 残留 casting 字段）

    # 触发条件：CASTING 状态下被打断。v2 直接清状态但忘了退款 mp，
    #          且 cast_remaining 没归零（残留浮点）。

    # 抓得到的不变式：
    #   I-08-focus  interrupt_refunds_mp  (meta.mp_refunded != True)
    #   （I-07 interrupt_clears_casting 仍能通过——casting_skill 我们清了）

    def on_interrupt(self, sim: PySim, actor: Character, skill_id: str) -> None:
        # 注意：我们 没有 退款 mp。
        # spec = sim.skills.get(skill_id)
        # actor.mp += spec.mp_cost   # ← v1 的这一行被"优化"掉了
        actor.state = CharacterState.IDLE
        actor.casting_skill = None
        # actor.cast_remaining = 0.0  ← 也忘了归零
        sim.emit(
            kind="cast_interrupted",
            actor=actor.id,
            skill=skill_id,
            # ---- BUG-003：mp_refunded 标记错误地设为 True，掩盖了实际未退款 ----
            # 这种"日志说一套实际做一套"是真实 bug 报告里最让 QA 头疼的场景
            meta={"mp_refunded": False},

        )

    # BUG-005 · 暴击 RNG 没走 sandbox 的全局 seed

    # 触发条件：任意伤害结算。v2 的暴击判定使用了 random.random()
    #          全局 RNG，而非 sim.rng（带 seed 注入的）。

    # 后果：相同 seed 的两次跑产出不同 trace（damage 可能 crit 也可能不 crit），
    #       破坏 replay determinism。

    # 抓得到的不变式：
    #   I-10  replay_deterministic（D8/D12 实现专用 evaluator 后能抓）
    # 同时也间接抓到 BUG-002 / BUG-004（它们叠加进 damage 公式）

    # 为了不影响其它 bug 的演示路径，我们在 on_cast_complete 里完整复刻 v1
    # 的逻辑，但把 sim.rng 换成 random.random()。

    def on_cast_complete(
        self, sim: PySim, actor: Character, skill_id: str, target_id: str
    ) -> None:
        spec = sim.skills.get(skill_id)
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

        if spec.damage_base > 0:
            target = sim.state().characters.get(target_id, actor)
            # ---- BUG-005：用全局 random.random() 而非 sim.rng ----
            crit = random.random() < CRIT_CHANCE

            dmg = spec.damage_base * (CRIT_MULT if crit else 1.0)
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

        for buff_id in spec.self_buffs:
            _v2_apply_buff(sim, actor, actor, buff_id)
        for buff_id in spec.target_buffs:
            target = sim.state().characters.get(target_id, actor)
            _v2_apply_buff(sim, actor, target, buff_id)

# --- BUG-002 · refresh 错误地累加 magnitude ---
# 触发条件：同一 buff 在仍存活时被再次施加（refresh 语义）。
# v2 在 refresh 分支错误地把 magnitude 加到旧值上，而不是替换。
# 抓得到的不变式：
#   I-05-chilled / I-05-burn / I-05-arcane-power
#     buff_refresh_magnitude_stable

def _v2_apply_buff(sim: PySim, source: Character, target: Character, buff_id: str) -> None:
    spec = sim.buffs.get(buff_id)
    existing = next((b for b in target.buffs if b.spec_id == buff_id), None)

    if existing is not None and spec.stack_rule == StackRule.REFRESH:
        existing.remaining = spec.duration
        # ---- BUG-002：refresh 时 magnitude 累加而不是替换 ----
        existing.magnitude = existing.magnitude + spec.magnitude
        # 真实游戏里这种 bug 很常见：策划本意是 "刷新持续时间"，
        # 程序员误读成 "再叠一层效果"，结果玩家释放第二次同样
        # 技能后伤害/冷却效果意外翻倍。

        sim.emit(
            kind="buff_refreshed",
            actor=target.id,
            buff=buff_id,
            amount=existing.magnitude,
        )
        return

    # 其余分支与 v1 保持一致
    _v1_apply_buff(sim, source, target, buff_id)

# --- BUG-004 · DoT 浮点累积误差（D8 落地） ---
# v1 的 on_dot_tick 精确计算 magnitude * dt 每 tick 扣血。v2 在那基础上乘了
# 一个错误的 1.05 "补偿系数"——"想修一个不存在的精度问题反而引入精度问题"
# 是真实游戏开发里很常见的 regression 类型。
#
# 4s burn × 80 ticks：
#   v1 总伤 = 10 * 4.0 = 40.0（精确，和 tick_dt 无关）
#   v2 总伤 = 40.0 * 1.05 = 42.0（漂移 5%）
#
# I-09 oracle（dot_total_damage_within_tolerance）容忍 0.5，2.0 的漂移必然
# 被抓到。

# 把 BUG-004 的 on_dot_tick 覆写挂到 V2SkillHandler 上（继承自 V1SkillHandler，
# 默认走 v1 的精确计算；下面的 monkey-patch 风格覆写让"故意改坏的代码"集中
# 体现在 v2/skills.py 里，便于面试讲故事）。
def _v2_on_dot_tick(
    self: V2SkillHandler,
    sim: PySim,
    target: Character,
    buff_spec_id: str,
    magnitude: float,
    dt: float,
) -> None:
    # ---- BUG-004：错误地乘了 1.05 ----
    damage = magnitude * dt * DOT_BUG_DRIFT_MULTIPLIER

    target.hp = max(0.0, target.hp - damage)
    sim.emit(
        kind="dot_tick",
        target=target.id,
        buff=buff_spec_id,
        amount=round(damage, 6),
        meta={"v2_drift_multiplier": DOT_BUG_DRIFT_MULTIPLIER},
    )
    if target.hp <= 0:
        target.state = CharacterState.DEAD
        sim.emit(kind="death", actor=target.id)

V2SkillHandler.on_dot_tick = _v2_on_dot_tick  # type: ignore[assignment]
