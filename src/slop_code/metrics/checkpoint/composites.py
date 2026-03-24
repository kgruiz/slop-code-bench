"""Checkpoint-owned composite metric helpers."""

from __future__ import annotations

from typing import Any


def compute_checkpoint_verbosity(
    metrics: dict[str, Any],
) -> float | None:
    """Compute checkpoint verbosity from saved checkpoint metrics."""
    verbosity_flagged_pct = metrics.get("verbosity_flagged_pct")
    if isinstance(verbosity_flagged_pct, int | float):
        return float(verbosity_flagged_pct)

    loc = metrics.get("loc")
    if not isinstance(loc, int | float) or loc <= 0:
        return None

    clone_lines = metrics.get("clone_lines", 0)
    violation_pct = metrics.get("violation_pct")
    if not isinstance(clone_lines, int | float) or not isinstance(
        violation_pct, int | float
    ):
        return None

    clone_ratio = clone_lines / loc
    return clone_ratio + float(violation_pct)


def compute_checkpoint_erosion(
    metrics: dict[str, Any],
) -> float | None:
    """Compute checkpoint erosion from the saved high-CC mass ratio."""
    score = metrics.get("mass.high_cc_pct")
    if not isinstance(score, int | float):
        return None

    if 0.0 <= score <= 1.0:
        return float(score)
    return None
