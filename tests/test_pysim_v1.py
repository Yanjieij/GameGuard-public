"""D1–D2 smoke tests for PySim v1.

These tests play the role of the 'REPL sanity check' the plan milestones
call for. They verify:
    - a full cast cycle: mp cost, cast time, cooldown start, damage event
    - buff refresh semantics (I-05)
    - interrupt refund semantics (I-07, I-08)
    - replay determinism (I-10)
    - invariant evaluators match reality on a clean v1 run
"""
from __future__ import annotations

from gameguard.domain import (
    CastAction,
    CharacterState,
    InterruptAction,
    WaitAction,
)
from gameguard.domain.invariant import (
    BuffRefreshMagnitudeStableInvariant,
    CooldownAtLeastAfterCastInvariant,
    InterruptClearsCastingInvariant,
    InterruptRefundsMpInvariant,
    StateView,
    evaluate,
)
from gameguard.sandbox.pysim.factory import make_sandbox


def _view(sim) -> StateView:
    s = sim.state()
    return StateView(t=s.t, tick=s.tick, characters=dict(s.characters))


def test_fireball_full_cast_cycle() -> None:
    sim = make_sandbox("v1")
    sim.reset(seed=42)

    # Starting state
    s0 = sim.state()
    assert s0.characters["p1"].mp == 100.0
    assert s0.characters["dummy"].hp == 1000.0

    # Cast Fireball -> one tick advances (cast_start)
    r = sim.step(CastAction(actor="p1", skill="skill_fireball", target="dummy"))
    assert r.outcome.accepted, r.outcome.reason
    assert sim.state().characters["p1"].state == CharacterState.CASTING
    assert sim.state().characters["p1"].mp == 70.0        # 100 - 30
    # Cast should still be in progress (cast_time=1.0 > one tick of 0.05s)
    assert sim.state().characters["p1"].cast_remaining > 0.9

    # Wait 1.0s to finish cast_time
    sim.step(WaitAction(seconds=1.0))
    p1 = sim.state().characters["p1"]
    assert p1.state == CharacterState.IDLE
    assert p1.casting_skill is None
    assert p1.cooldowns.get("skill_fireball", 0.0) > 7.9  # cooldown started

    # Damage event should exist and HP dropped
    dmg_events = sim.trace().of_kind("damage_dealt")
    assert len(dmg_events) == 1
    assert sim.state().characters["dummy"].hp < 1000.0

    cast_complete = sim.trace().of_kind("cast_complete")
    assert len(cast_complete) == 1


def test_cooldown_decreases_over_time() -> None:
    sim = make_sandbox("v1")
    sim.reset(seed=1)
    sim.step(CastAction(actor="p1", skill="skill_fireball", target="dummy"))
    sim.step(WaitAction(seconds=1.0))     # cast completes, CD = 8.0

    cd0 = sim.state().characters["p1"].cooldown_remaining("skill_fireball")
    sim.step(WaitAction(seconds=2.0))
    cd1 = sim.state().characters["p1"].cooldown_remaining("skill_fireball")

    # Should have decreased by roughly 2.0s.
    assert cd0 - cd1 > 1.9
    assert cd0 - cd1 < 2.1

    # Invariant check
    inv = CooldownAtLeastAfterCastInvariant(
        id="I-03", description="cd decreases correctly", actor="p1",
        skill="skill_fireball", expected_cooldown=8.0,
    )
    res = evaluate(inv, _view(sim), sim.trace())
    assert res.passed, res.message


def test_frostbolt_applies_chilled_and_refresh_does_not_inflate_magnitude() -> None:
    sim = make_sandbox("v1")
    sim.reset(seed=7)

    sim.step(CastAction(actor="p1", skill="skill_frostbolt", target="dummy"))
    sim.step(WaitAction(seconds=1.5))
    chilled = [b for b in sim.state().characters["dummy"].buffs if b.spec_id == "buff_chilled"]
    assert len(chilled) == 1
    assert abs(chilled[0].magnitude - 0.3) < 1e-6

    # wait long enough for CD to expire
    sim.step(WaitAction(seconds=6.0))
    sim.step(CastAction(actor="p1", skill="skill_frostbolt", target="dummy"))
    sim.step(WaitAction(seconds=1.5))

    chilled = [b for b in sim.state().characters["dummy"].buffs if b.spec_id == "buff_chilled"]
    assert len(chilled) == 1
    # Magnitude must still equal the spec value.
    assert abs(chilled[0].magnitude - 0.3) < 1e-6

    inv = BuffRefreshMagnitudeStableInvariant(
        id="I-05", description="refresh does not stack magnitude",
        actor="dummy", buff="buff_chilled", expected_magnitude=0.3,
    )
    assert evaluate(inv, _view(sim), sim.trace()).passed


def test_interrupt_clears_casting_and_refunds_mp() -> None:
    sim = make_sandbox("v1")
    sim.reset(seed=2)

    # Start a long cast (Arcane Focus: 2.0s)
    mp_before = sim.state().characters["p1"].mp
    sim.step(CastAction(actor="p1", skill="skill_focus", target="p1"))
    assert sim.state().characters["p1"].state == CharacterState.CASTING
    assert sim.state().characters["p1"].mp == mp_before - 20.0

    sim.step(WaitAction(seconds=0.5))
    sim.step(InterruptAction(actor="p1"))

    p1 = sim.state().characters["p1"]
    assert p1.state == CharacterState.IDLE
    assert p1.casting_skill is None
    assert abs(p1.mp - mp_before) < 1e-6              # fully refunded
    assert "skill_focus" not in p1.cooldowns           # no cooldown penalty

    interrupted = sim.trace().of_kind("cast_interrupted")
    assert len(interrupted) == 1
    assert interrupted[0].meta.get("mp_refunded") is True

    inv_clear = InterruptClearsCastingInvariant(
        id="I-07", description="interrupt clears casting field", actor="p1"
    )
    inv_refund = InterruptRefundsMpInvariant(
        id="I-08", description="interrupt refunds mp", actor="p1", skill="skill_focus"
    )
    assert evaluate(inv_clear, _view(sim), sim.trace()).passed
    assert evaluate(inv_refund, _view(sim), sim.trace()).passed


def test_replay_determinism() -> None:
    """Same seed + same actions -> identical event sequences (I-10)."""
    actions = [
        CastAction(actor="p1", skill="skill_frostbolt", target="dummy"),
        WaitAction(seconds=2.0),
        CastAction(actor="p1", skill="skill_ignite", target="dummy"),
        WaitAction(seconds=5.0),
    ]

    def run_once() -> list[tuple]:
        sim = make_sandbox("v1")
        sim.reset(seed=123)
        for a in actions:
            sim.step(a)
        return [
            (e.tick, e.kind, e.actor, e.target, e.skill, e.buff, e.amount)
            for e in sim.trace().events
        ]

    assert run_once() == run_once()


def test_snapshot_restore_round_trip() -> None:
    sim = make_sandbox("v1")
    sim.reset(seed=9)
    sim.step(CastAction(actor="p1", skill="skill_frostbolt", target="dummy"))
    sim.step(WaitAction(seconds=1.5))

    snap = sim.snapshot()
    state_before = sim.state().model_dump()
    log_len_before = len(sim.trace())

    # Mutate, then restore.
    sim.step(WaitAction(seconds=10.0))
    assert sim.state().t > state_before["t"]

    sim.restore(snap)
    assert sim.state().model_dump() == state_before
    assert len(sim.trace()) == log_len_before
