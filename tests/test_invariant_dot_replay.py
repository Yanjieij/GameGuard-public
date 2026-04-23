"""D8 meta-tests —— I-09 (DoT 总伤) + I-10 (replay determinism)。

差分模式：每条 oracle 在 v1 上 PASS，在 v2 上 FAIL。
"""
from __future__ import annotations

from gameguard.domain import CastAction, WaitAction
from gameguard.domain.invariant import (
    DotTotalDamageWithinToleranceInvariant,
    ReplayDeterministicInvariant,
    StateView,
    evaluate,
)
from gameguard.sandbox.pysim.factory import make_sandbox
from gameguard.testcase.model import (
    Assertion,
    AssertionWhen,
    CaseOutcome,
    TestCase,
    TestPlan,
)
from gameguard.testcase.runner import run_plan


def _view(sim):
    s = sim.state()
    return StateView(t=s.t, tick=s.tick, characters=dict(s.characters))


# --------------------------------------------------------------------------- #
# I-09 DoT 总伤
# --------------------------------------------------------------------------- #


def test_i09_dot_total_v1_within_tolerance() -> None:
    """v1 burn 4s 总伤 ≈ magnitude * duration = 40，浮点边界容许 +0.5。
    默认 tolerance=1.0 应通过。
    """
    sim = make_sandbox("v1")
    sim.reset(seed=1)
    sim.step(CastAction(actor="p1", skill="skill_ignite", target="dummy"))
    sim.step(WaitAction(seconds=4.5))   # 跑完整个 burn 4s + buffer

    inv = DotTotalDamageWithinToleranceInvariant(
        id="I-09-burn-v1", description="burn 总伤 ≈ 40", actor="dummy",
        buff="buff_burn", expected_total=40.0,
    )
    res = evaluate(inv, _view(sim), sim.trace())
    assert res.passed, res.message
    # 实际值约 40.5（多了一个 tick 的浮点误差）
    assert isinstance(res.actual, type(None)) or 40 <= float(res.actual or 0) < 41


def test_i09_dot_total_v2_drift() -> None:
    """v2 burn 4s 总伤 = 40 * 1.05 ≈ 42.5（漂移 5%）。oracle 应失败。"""
    sim = make_sandbox("v2")
    sim.reset(seed=1)
    sim.step(CastAction(actor="p1", skill="skill_ignite", target="dummy"))
    sim.step(WaitAction(seconds=4.5))

    inv = DotTotalDamageWithinToleranceInvariant(
        id="I-09-burn-v2", description="burn 总伤", actor="dummy",
        buff="buff_burn", expected_total=40.0,
    )
    res = evaluate(inv, _view(sim), sim.trace())
    assert not res.passed, "BUG-004 应触发 I-09 oracle"
    assert "DoT" in res.message
    # 实际值应在 42 - 43 区间（v2 漂移 5%）
    assert isinstance(res.actual, float)
    assert 42.0 < res.actual < 43.0


def test_dot_tick_events_not_in_v1_when_no_burn() -> None:
    """非 burn 用例不应产 dot_tick 事件（确保 DoT-on-tick 没污染其它路径）。"""
    sim = make_sandbox("v1")
    sim.reset(seed=1)
    sim.step(CastAction(actor="p1", skill="skill_fireball", target="dummy"))
    sim.step(WaitAction(seconds=2.0))
    assert sim.trace().of_kind("dot_tick") == []


# --------------------------------------------------------------------------- #
# I-10 replay determinism
# --------------------------------------------------------------------------- #


def _make_replay_case(version: str) -> TestCase:
    return TestCase(
        id=f"replay-{version}",
        name=f"replay {version}",
        sandbox=f"pysim:{version}",
        seed=42,
        actions=[
            CastAction(actor="p1", skill="skill_fireball", target="dummy"),
            WaitAction(seconds=1.0),
            CastAction(actor="p1", skill="skill_frostbolt", target="dummy"),
            WaitAction(seconds=2.0),
        ],
        assertions=[
            Assertion(
                invariant=ReplayDeterministicInvariant(
                    id=f"I-10-{version}",
                    description=f"v{version} replay 确定性",
                ),
                when=AssertionWhen.END_OF_RUN,
            )
        ],
    )


def _factory(spec: str):
    _, version = spec.split(":", 1)
    return make_sandbox(version)


def test_i10_replay_v1_passes(tmp_path) -> None:
    """v1 双跑应当 byte-for-byte 一致。"""
    case = _make_replay_case("v1")
    plan = TestPlan(id="meta", cases=[case])
    suite = run_plan(plan, _factory, artifacts_dir=tmp_path)
    r = suite.cases[0]
    assert r.outcome == CaseOutcome.PASSED, (
        f"v1 应当确定性可重放，但失败了：{r.failing_assertions}"
    )


def test_i10_replay_v2_fails_for_bug005(tmp_path) -> None:
    """v2 因 BUG-005（暴击用 global random）每次 crit 序列不同 → 应失败。

    注意：BUG-005 只在有 damage_dealt 事件时才暴露 crit 字段差异；
    我们的 replay case 至少产 2 次 fireball/frostbolt 伤害事件，2^2 = 4 种
    crit 组合，两次跑不同的概率很高。极低概率两次 crit 完全相同时，会假阴性。
    我们多跑一遍提升信号。
    """
    case = _make_replay_case("v2")
    plan = TestPlan(id="meta", cases=[case])
    # 两次 run_plan 调用的 _check_replay_determinism 各自完成一次双跑；
    # 任何一次双跑里序列不同就足够。
    found_failure = False
    for _ in range(3):
        suite = run_plan(plan, _factory, artifacts_dir=tmp_path)
        if suite.cases[0].outcome == CaseOutcome.FAILED:
            found_failure = True
            break
    assert found_failure, "BUG-005 应让 v2 双跑产生不同序列（3 次重试都未抓到）"
