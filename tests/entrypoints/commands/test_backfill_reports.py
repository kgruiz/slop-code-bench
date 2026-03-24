from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

from slop_code.entrypoints.commands.backfill_reports import (
    _update_ast_grep_jsonl,
)


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.write_text("".join(json.dumps(row) + "\n" for row in rows))


def test_update_ast_grep_jsonl_filters_out_unknown_rules(
    tmp_path: Path,
) -> None:
    quality_dir = tmp_path / "quality_analysis"
    quality_dir.mkdir()
    jsonl_path = quality_dir / "ast_grep.jsonl"
    overall_path = quality_dir / "overall_quality.json"

    _write_jsonl(
        jsonl_path,
        [
            {
                "rule_id": "manual-sum-loop",
                "category": "verbosity",
                "subcategory": "verbosity",
                "weight": 4,
                "line": 1,
                "end_line": 1,
            },
            {
                "rule_id": "bare-except-pass",
                "category": "safety",
                "subcategory": "safety",
                "weight": 4,
                "line": 2,
                "end_line": 2,
            },
        ],
    )
    overall_path.write_text(json.dumps({"ast_grep": {}}))

    rules_lookup = {
        "manual-sum-loop": {
            "category": "slop",
            "subcategory": "slop",
            "weight": 4,
        }
    }

    total, updated = _update_ast_grep_jsonl(
        jsonl_path,
        rules_lookup,
        MagicMock(),
    )

    assert total == 1
    assert updated == 1

    lines = [json.loads(line) for line in jsonl_path.read_text().splitlines()]
    assert lines == [
        {
            "rule_id": "manual-sum-loop",
            "category": "slop",
            "subcategory": "slop",
            "weight": 4,
            "line": 1,
            "end_line": 1,
        }
    ]

    overall = json.loads(overall_path.read_text())
    assert overall["ast_grep"]["violations"] == 1
    assert overall["ast_grep"]["category_counts"] == {"slop": 1}
    assert overall["ast_grep"]["category_weighted"] == {"slop": 4}
