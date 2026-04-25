from __future__ import annotations

from pathlib import Path

import pytest

from evals.design_doc.eval_design_doc import (
    EVAL_CASES,
    InvariantKey,
    load_golden,
    score_bundle,
)
from gameguard.domain.invariant import (
    DotTotalDamageWithinToleranceInvariant,
    HpNonnegInvariant,
    InvariantBundle,
    ReplayDeterministicInvariant,
)


def test_design_doc_golden_totals_are_self_consistent() -> None:
    for case in EVAL_CASES.values():
        required, optional = load_golden(case.golden_path)
        assert required
        assert not (required & optional)


def test_load_golden_rejects_stale_declared_total(tmp_path: Path) -> None:
    golden = tmp_path / "golden.yaml"
    golden.write_text(
        """
required:
  - kind: hp_nonneg
    actor: p1
total_required: 2
""".strip(),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="total_required"):
        load_golden(golden)


def test_score_bundle_counts_dot_and_replay_kinds() -> None:
    required = {
        InvariantKey(
            kind="dot_total_damage_within_tolerance",
            actor="dummy",
            buff="buff_poison",
        ),
        InvariantKey(kind="replay_deterministic"),
    }
    optional = {InvariantKey(kind="hp_nonneg", actor="p1")}
    bundle = InvariantBundle(
        items=[
            DotTotalDamageWithinToleranceInvariant(
                id="dot",
                description="poison total damage",
                actor="dummy",
                buff="buff_poison",
                expected_total=40.0,
            ),
            ReplayDeterministicInvariant(
                id="replay",
                description="same seed emits same event log",
            ),
            HpNonnegInvariant(id="hp", description="hp nonnegative", actor="p1"),
        ]
    )

    scored = score_bundle(bundle, required, optional)

    assert scored["recall"] == 1.0
    assert scored["precision"] == 1.0
    assert scored["hit_required_count"] == 2
    assert scored["hit_optional_count"] == 1
