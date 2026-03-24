from __future__ import annotations

from slop_code.entrypoints.commands.consolidate_runs import EXPECTED_MASS_COLS
from slop_code.entrypoints.commands.consolidate_runs import TEST_COLS
from slop_code.entrypoints.commands.consolidate_runs import check_mass_columns


def _build_complete_mass_record() -> dict[str, float]:
    return dict.fromkeys(EXPECTED_MASS_COLS, 1.0)


def test_check_mass_columns_passes_when_all_expected_present():
    record = _build_complete_mass_record()

    assert check_mass_columns(record) is False


def test_check_mass_columns_fails_when_complexity_concentration_missing():
    record = _build_complete_mass_record()
    del record["mass.cc"]

    assert check_mass_columns(record) is True


def test_check_mass_columns_only_requires_cc_mass():
    record = _build_complete_mass_record()
    record["mass.unused"] = 1.0

    assert check_mass_columns(record) is False


def test_test_cols_use_new_pass_rate_names_and_drop_elapsed():
    assert "strict_pass_rate" in TEST_COLS
    assert "isolated_pass_rate" in TEST_COLS
    assert "pass_rate" not in TEST_COLS
    assert "checkpoint_pass_rate" not in TEST_COLS
    assert "elapsed" not in TEST_COLS
