"""Tests for mass helpers and the reduced CC mass metric."""

from __future__ import annotations

import pytest

from slop_code.metrics.checkpoint.mass import _compute_gini_coefficient
from slop_code.metrics.checkpoint.mass import compute_mass_metrics
from slop_code.metrics.checkpoint.mass import compute_top20_share


class TestComputeGiniCoefficient:
    def test_uniform_distribution_returns_zero(self):
        assert _compute_gini_coefficient([10.0] * 100) == 0.0

    def test_empty_list_returns_zero(self):
        assert _compute_gini_coefficient([]) == 0.0

    def test_known_distribution(self):
        gini = _compute_gini_coefficient([1.0, 2.0, 3.0, 4.0, 5.0])
        assert 0.26 < gini < 0.28


class TestComputeTop20Share:
    def test_empty_list_returns_zero(self):
        assert compute_top20_share([]) == 0.0

    def test_uniform_distribution(self):
        values = [10.0] * 100
        assert compute_top20_share(values) == pytest.approx(0.20, abs=0.01)

    def test_concentrated_distribution(self):
        values = [100.0] + [0.001] * 99
        assert compute_top20_share(values) > 0.95


class TestComputeMassMetrics:
    def test_returns_cc_mass_and_high_cc_pct(self):
        result = compute_mass_metrics(
            iter(
                [
                    {
                        "type": "function",
                        "complexity": 5,
                        "sloc": 9,
                    },
                    {
                        "type": "method",
                        "complexity": 16,
                        "sloc": 25,
                    },
                    {
                        "type": "class",
                        "complexity": 99,
                        "sloc": 100,
                    },
                ]
            )
        )

        assert result["mass.cc"] == pytest.approx(95.0)
        assert result["mass.high_cc_pct"] == pytest.approx(0.8421, abs=0.0001)

    def test_uses_true_sloc_not_lines_or_statements(self):
        result = compute_mass_metrics(
            iter(
                [
                    {
                        "type": "function",
                        "complexity": 2,
                        "sloc": 100,
                        "lines": 1,
                        "statements": 1,
                    },
                    {
                        "type": "function",
                        "complexity": 20,
                        "sloc": 1,
                        "lines": 1000,
                        "statements": 100,
                    },
                ]
            )
        )

        assert result["mass.cc"] == pytest.approx(40.0)
        assert result["mass.high_cc_pct"] == pytest.approx(0.5)

    def test_zero_or_missing_sloc_contributes_no_mass(self):
        result = compute_mass_metrics(
            iter(
                [
                    {"type": "function", "complexity": 7, "sloc": 0},
                    {"type": "method", "complexity": 4},
                ]
            )
        )

        assert result == {"mass.cc": 0.0, "mass.high_cc_pct": 0.0}

    def test_empty_symbols_returns_zero_mass(self):
        assert compute_mass_metrics(iter([])) == {
            "mass.cc": 0.0,
            "mass.high_cc_pct": 0.0,
        }
