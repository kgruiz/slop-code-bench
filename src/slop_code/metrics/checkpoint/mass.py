"""Mass metrics for checkpoint exports."""

from __future__ import annotations

import math
from collections.abc import Iterator
from typing import Any


def _compute_gini_coefficient(masses: list[float]) -> float:
    """Compute the Gini coefficient for a non-negative distribution."""
    non_zero = [mass for mass in masses if mass > 1e-9]
    if len(non_zero) <= 1:
        return 0.0

    sorted_masses = sorted(non_zero)
    total = sum(sorted_masses)
    if total < 1e-9:
        return 0.0

    weighted_sum = sum(
        (index + 1) * value for index, value in enumerate(sorted_masses)
    )
    count = len(sorted_masses)
    return (2 * weighted_sum - (count + 1) * total) / (count * total)


def compute_top20_share(values: list[float]) -> float:
    """Return the share of total value held by the top 20 percent."""
    non_zero = [value for value in values if value > 1e-9]
    if len(non_zero) <= 1:
        return 0.0

    sorted_desc = sorted(non_zero, reverse=True)
    top_count = max(1, math.ceil(0.2 * len(sorted_desc)))
    return sum(sorted_desc[:top_count]) / sum(sorted_desc)


def compute_mass_metrics(
    symbol_iter: Iterator[dict[str, Any]],
) -> dict[str, float]:
    """Compute CC mass using true symbol SLOC."""
    total_mass = 0.0
    high_cc_mass = 0.0

    for symbol in symbol_iter:
        if symbol.get("type") not in {"function", "method"}:
            continue

        sloc = symbol.get("sloc", 0)
        if sloc <= 0:
            continue

        mass = symbol.get("complexity", 0) * math.sqrt(sloc)
        total_mass += mass
        if symbol.get("complexity", 0) > 10:
            high_cc_mass += mass

    high_cc_pct = (high_cc_mass / total_mass) if total_mass else 0.0
    return {
        "mass.cc": round(total_mass, 2),
        "mass.high_cc_pct": high_cc_pct,
    }
