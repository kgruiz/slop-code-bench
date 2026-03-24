from __future__ import annotations

import pytest

from slop_code.metrics.checkpoint.composites import compute_checkpoint_erosion
from slop_code.metrics.checkpoint.composites import compute_checkpoint_verbosity


def test_compute_checkpoint_verbosity_uses_violation_pct_and_clone_ratio_only():
    metrics = {
        "loc": 100,
        "clone_lines": 20,
        "violation_pct": 0.15,
        "verbosity_flagged_pct": 0.22,
        "functions": 10,
        "methods": 5,
        "trivial_wrappers": 99,
        "single_use_functions": 99,
    }

    result = compute_checkpoint_verbosity(metrics)

    assert result == pytest.approx(0.22)


def test_compute_checkpoint_erosion_preserves_high_cc_pct_ratio():
    metrics = {"mass.high_cc_pct": 0.42}

    result = compute_checkpoint_erosion(metrics)

    assert result == pytest.approx(0.42)


def test_compute_checkpoint_erosion_accepts_zero_to_one_only():
    assert compute_checkpoint_erosion(
        {"mass.high_cc_pct": 1.0}
    ) == pytest.approx(1.0)
    assert compute_checkpoint_erosion({"mass.high_cc_pct": 100.0}) is None
