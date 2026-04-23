"""D6 meta-tests —— v2 的每个植入 bug 都能用一条直接 oracle 抓到。

==============================================================================
为什么需要这一组 meta-test？
==============================================================================

v2 是一个"演示工具"：它的价值在于**确实有 bug、且 bug 能被某个不变式
抓到**。如果某个 bug 写错了（比如本意触发 BUG-002 但实际改成了不影响
任何 oracle），那 demo 就废了。

所以这一组测试把每个 BUG-xxx 与"应该被它触发的 invariant"配对验证：
  - 在 v1 上跑：不变式必须 PASS（证明 oracle 自身不是错的）
  - 在 v2 上跑：不变式必须 FAIL（证明 bug 真的被植入了）

这是经典的 "differential testing" 模式：用 v1 当 ground truth，差异即 bug。
真实工业里 Ubisoft、Blizzard、米哈游内部回归都在这条路径上。
"""
from __future__ import annotations

from gameguard.domain import (
    CastAction,
    InterruptAction,
    WaitAction,
)
from gameguard.domain.invariant import (
    BuffRefreshMagnitudeStableInvariant,
    CooldownAtLeastAfterCastInvariant,
    InterruptRefundsMpInvariant,
    StateView,
    evaluate,
)
from gameguard.sandbox.pysim.factory import make_sandbox


def _view(sim) -> StateView:
    s = sim.state()
    return StateView(t=s.t, tick=s.tick, characters=dict(s.characters))


# --------------------------------------------------------------------------- #
# v2 自身能跑（不崩、动作被接受）
# --------------------------------------------------------------------------- #


def test_v2_can_run_basic_cast() -> None:
    sim = make_sandbox("v2")
    sim.reset(seed=1)
    r = sim.step(CastAction(actor="p1", skill="skill_fireball", target="dummy"))
    assert r.outcome.accepted, r.outcome.reason
    sim.step(WaitAction(seconds=1.0))
    # 至少完成了一次伤害事件
    assert sim.trace().of_kind("damage_dealt"), "v2 应能完成基础施法"


# --------------------------------------------------------------------------- #
# BUG-001：cooldown 错误重置
# --------------------------------------------------------------------------- #


def test_bug001_cooldown_reset_on_v2_only() -> None:
    """v1 通过、v2 失败：切技能瞬间 v2 把所有冷却清零。"""
    inv = CooldownAtLeastAfterCastInvariant(
        id="I-04-fireball",
        description="切换技能不应重置 fireball 冷却",
        actor="p1",
        skill="skill_fireball",
        expected_cooldown=8.0,
        tolerance=0.1,
    )

    actions = [
        CastAction(actor="p1", skill="skill_fireball", target="dummy"),
        WaitAction(seconds=1.0),     # fireball 完成 -> CD=8
        CastAction(actor="p1", skill="skill_frostbolt", target="dummy"),
        WaitAction(seconds=1.5),
    ]

    # ---- v1：oracle 应通过 ----
    sim = make_sandbox("v1")
    sim.reset(seed=1)
    for a in actions:
        sim.step(a)
    assert evaluate(inv, _view(sim), sim.trace()).passed, "BUG-001 oracle 在 v1 上不应触发"

    # ---- v2：oracle 应失败 ----
    sim = make_sandbox("v2")
    sim.reset(seed=1)
    for a in actions:
        sim.step(a)
    res = evaluate(inv, _view(sim), sim.trace())
    assert not res.passed, "BUG-001 应在 v2 上被 oracle 抓到"
    assert "cooldown" in res.message.lower()


# --------------------------------------------------------------------------- #
# BUG-002：buff refresh 累加 magnitude
# --------------------------------------------------------------------------- #


def test_bug002_buff_refresh_magnitude_on_v2_only() -> None:
    inv = BuffRefreshMagnitudeStableInvariant(
        id="I-05-chilled",
        description="chilled refresh 后 magnitude 保持 0.3",
        actor="dummy",
        buff="buff_chilled",
        expected_magnitude=0.3,
    )

    actions = [
        CastAction(actor="p1", skill="skill_frostbolt", target="dummy"),
        WaitAction(seconds=1.5),
        WaitAction(seconds=6.0),     # 等 CD
        CastAction(actor="p1", skill="skill_frostbolt", target="dummy"),
        WaitAction(seconds=1.5),
    ]

    sim = make_sandbox("v1")
    sim.reset(seed=11)
    for a in actions:
        sim.step(a)
    assert evaluate(inv, _view(sim), sim.trace()).passed

    # v2: BUG-001 也会被同时触发（切 frostbolt 时清空了 fireball 的 CD），
    # 但本用例没用 fireball，所以 BUG-001 不影响断言。
    # 然而 BUG-001 的 cooldowns.clear() 会让第二次 frostbolt 的 cooldown 也被清零；
    # 不过它发生在 cast_start 时，第二次 cast_complete 后 CD 又会被重新塞回，
    # 所以最终我们关心的是 buff_chilled 的 magnitude——预期 v2 把它 0.3+0.3=0.6。
    sim = make_sandbox("v2")
    sim.reset(seed=11)
    for a in actions:
        sim.step(a)
    res = evaluate(inv, _view(sim), sim.trace())
    assert not res.passed, "BUG-002 应在 v2 上被 oracle 抓到"
    assert "magnitude" in res.message.lower()


# --------------------------------------------------------------------------- #
# BUG-003：打断未退款 mp
# --------------------------------------------------------------------------- #


def test_bug003_interrupt_no_mp_refund_on_v2_only() -> None:
    inv = InterruptRefundsMpInvariant(
        id="I-08-focus",
        description="打断 focus 应退款 mp",
        actor="p1",
        skill="skill_focus",
    )

    actions = [
        CastAction(actor="p1", skill="skill_focus", target="p1"),
        WaitAction(seconds=0.5),
        InterruptAction(actor="p1"),
    ]

    sim = make_sandbox("v1")
    sim.reset(seed=21)
    for a in actions:
        sim.step(a)
    assert evaluate(inv, _view(sim), sim.trace()).passed

    sim = make_sandbox("v2")
    sim.reset(seed=21)
    for a in actions:
        sim.step(a)
    res = evaluate(inv, _view(sim), sim.trace())
    assert not res.passed, "BUG-003 应在 v2 上被 oracle 抓到"
    assert "mp_refunded" in res.message.lower()


# --------------------------------------------------------------------------- #
# BUG-005：暴击 RNG 没走 sandbox seed -> 重放不一致
# --------------------------------------------------------------------------- #


def test_bug005_replay_nondeterminism_on_v2() -> None:
    """v1：相同 seed -> 完全相同 trace。
    v2：相同 seed 跑两次，crit 序列因为用了全局 random.random()，**应该**不同。

    注意：因为用的是 global random，第一次跑前我们设一个 fixed global seed，
    确保至少证明 "v2 的暴击不依赖 sim seed"——通过两次跑同 sim seed
    但两次跑之间不重置 global random，trace 会偏移。
    """
    actions = [
        CastAction(actor="p1", skill="skill_fireball", target="dummy"),
        WaitAction(seconds=1.0),
    ]

    def crits_in_v(version: str, sim_seed: int) -> tuple[bool, ...]:
        sim = make_sandbox(version)
        sim.reset(seed=sim_seed)
        for a in actions:
            sim.step(a)
        return tuple(
            bool(e.meta.get("crit"))
            for e in sim.trace().of_kind("damage_dealt")
        )

    # v1：sim seed 决定 crit
    v1_run_a = crits_in_v("v1", 0)
    v1_run_b = crits_in_v("v1", 0)
    assert v1_run_a == v1_run_b, "v1 应当确定性可重放"

    # v2：sim seed 不影响 crit；我们手动喂不同 global seed，
    # 验证 v2 的 crit 真的取决于 global 而非 sim
    import random
    random.seed(7)
    v2_a = crits_in_v("v2", 0)
    random.seed(13)
    v2_b = crits_in_v("v2", 0)
    # 在不同 global seed 下，至少多次施放里某次 crit 状态会变。
    # 单次 fireball 撞同样结果的概率不为零，所以多打几次提升信号：
    if v2_a == v2_b:
        # 加大动作数再试一次，过滤偶然命中
        long_actions = []
        for _ in range(8):
            long_actions.extend(actions)
            long_actions.append(WaitAction(seconds=8.0))  # 等 fireball 冷却

        def crits_long(global_seed: int) -> tuple[bool, ...]:
            random.seed(global_seed)
            sim = make_sandbox("v2")
            sim.reset(seed=0)
            for a in long_actions:
                sim.step(a)
            return tuple(bool(e.meta.get("crit")) for e in sim.trace().of_kind("damage_dealt"))

        a2 = crits_long(7)
        b2 = crits_long(13)
        assert a2 != b2, "BUG-005 应让 v2 的 crit 序列不可由 sim seed 复现"
