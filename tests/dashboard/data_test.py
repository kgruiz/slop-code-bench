from __future__ import annotations

from slop_code.dashboard.data import process_checkpoint_row


def test_process_checkpoint_row_uses_new_pass_rate_names() -> None:
    processed = process_checkpoint_row(
        {
            "strict_pass_rate": 1.0,
            "isolated_pass_rate": 0.0,
            "total_tests": 10,
            "passed_tests": 5,
        }
    )

    assert processed["passed_chkpt"] is True
