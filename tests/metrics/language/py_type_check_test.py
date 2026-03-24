"""Tests for ty type checking integration."""

from __future__ import annotations

from textwrap import dedent
from unittest.mock import patch

from slop_code.metrics.languages.python.type_check import (
    calculate_type_check_metrics,
)


class TestCalculateTypeCheckMetrics:
    """Tests for calculate_type_check_metrics."""

    def test_clean_file(self, tmp_path):
        """File with no type errors returns zero metrics."""
        source = tmp_path / "clean.py"
        source.write_text(
            dedent("""
        x: int = 1
        y: str = "hello"
        """)
        )

        metrics = calculate_type_check_metrics(source)

        assert metrics.errors == 0
        assert metrics.warnings == 0

    def test_file_with_type_error(self, tmp_path):
        """File with a type error reports it."""
        source = tmp_path / "bad.py"
        source.write_text(
            dedent("""
        x: int = "hello"
        """)
        )

        metrics = calculate_type_check_metrics(source)

        assert metrics.errors >= 1
        assert "invalid-assignment" in metrics.counts

    def test_ty_not_available(self, tmp_path):
        """Gracefully returns empty metrics when ty is not installed."""
        source = tmp_path / "test.py"
        source.write_text("x = 1\n")

        with patch(
            "slop_code.metrics.languages.python.type_check.subprocess.run",
            side_effect=FileNotFoundError,
        ):
            metrics = calculate_type_check_metrics(source)

        assert metrics.errors == 0
        assert metrics.warnings == 0
        assert metrics.counts == {}

    def test_invalid_json_output(self, tmp_path):
        """Gracefully handles malformed ty output."""
        source = tmp_path / "test.py"
        source.write_text("x = 1\n")

        class FakeResult:
            stdout = "not json at all"
            returncode = 1

        with patch(
            "slop_code.metrics.languages.python.type_check.subprocess.run",
            return_value=FakeResult(),
        ):
            metrics = calculate_type_check_metrics(source)

        assert metrics.errors == 0
        assert metrics.warnings == 0

    def test_empty_file(self, tmp_path):
        """Empty file returns zero metrics."""
        source = tmp_path / "empty.py"
        source.write_text("")

        metrics = calculate_type_check_metrics(source)

        assert metrics.errors == 0
        assert metrics.warnings == 0
