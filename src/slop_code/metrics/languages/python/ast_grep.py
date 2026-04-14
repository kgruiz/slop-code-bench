"""AST-grep based pattern detection metrics."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from collections import Counter
from pathlib import Path

import yaml

from slop_code.logging import get_logger
from slop_code.metrics.models import AstGrepMetrics
from slop_code.metrics.models import AstGrepViolation

logger = get_logger(__name__)

RuleLookup = dict[str, dict[str, str | int]]

AST_GREP_CATEGORY = "slop"

# Default rules file (relative to project root).
AST_GREP_RULES_PATH = Path(__file__).parents[5] / "configs" / "slop_rules.yaml"
# Back-compat export; this now points at the single rules file.
AST_GREP_RULES_DIR = AST_GREP_RULES_PATH


def _is_ast_grep_binary(binary: str) -> bool:
    """Return whether a binary resolves to the ast-grep CLI."""

    try:
        result = subprocess.run(  # noqa: S603
            [binary, "--version"],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return False

    output = f"{result.stdout}\n{result.stderr}".lower()
    return "ast-grep" in output


def _get_ast_grep_binary() -> str | None:
    """Resolve the ast-grep CLI safely across platforms."""

    override = os.environ.get("AST_GREP_BIN")
    if override:
        return override if _is_ast_grep_binary(override) else None

    ast_grep = shutil.which("ast-grep")
    if ast_grep and _is_ast_grep_binary(ast_grep):
        return ast_grep

    sg = shutil.which("sg")
    if sg and _is_ast_grep_binary(sg):
        return sg

    return None


def _is_sg_available() -> bool:
    """Check if an ast-grep CLI is available on the system."""
    return _get_ast_grep_binary() is not None


def _get_ast_grep_rules_path() -> Path:
    """Get the AST-grep rules file, with env var override."""
    override = os.environ.get("AST_GREP_RULES_PATH")
    if override:
        return Path(override)
    override = os.environ.get("AST_GREP_RULES_DIR")
    if override:
        return Path(override)
    return AST_GREP_RULES_PATH


def _get_ast_grep_rules_dir() -> Path:
    """Back-compat wrapper returning the configured rules file path."""
    return _get_ast_grep_rules_path()


def _count_rules_in_file(rule_file: Path) -> int:
    """Return the number of AST-grep rules in a (possibly multi-doc) file."""
    try:
        with rule_file.open(encoding="utf-8") as handle:
            return sum(1 for doc in yaml.safe_load_all(handle) if doc)
    except (OSError, yaml.YAMLError) as exc:
        logger.debug(
            "Failed to read AST-grep rule file",
            rule_file=str(rule_file),
            error=str(exc),
        )
        return 0


def calculate_ast_grep_metrics(source: Path) -> AstGrepMetrics:
    """Calculate AST-grep metrics using ast-grep rules.

    Runs ast-grep scan with rules from the configured slop rules file
    and parses the JSON output to produce AstGrepMetrics.

    Args:
        source: Path to the Python source file.

    Returns:
        AstGrepMetrics with violations found, or empty metrics if ast-grep
        is unavailable.
    """
    ast_grep_bin = _get_ast_grep_binary()
    if ast_grep_bin is None:
        logger.debug("ast-grep not available, skipping ast-grep metrics")
        return AstGrepMetrics(
            violations=[], total_violations=0, counts={}, rules_checked=0
        )

    rules_path = _get_ast_grep_rules_path()
    if not rules_path.exists():
        logger.warning(
            "AST-grep rules file not found",
            rules_path=str(rules_path),
        )
        return AstGrepMetrics(
            violations=[], total_violations=0, counts={}, rules_checked=0
        )

    rules_checked = _count_rules_in_file(rules_path)

    if rules_checked == 0:
        return AstGrepMetrics(
            violations=[], total_violations=0, counts={}, rules_checked=0
        )

    # Build lookup to get correct category/subcategory/weight from rule files
    # (ast-grep's JSON output doesn't include rule metadata)
    rules_lookup = build_ast_grep_rules_lookup()

    violations: list[AstGrepViolation] = []
    counts: Counter[str] = Counter()
    logger.debug("Running ast-grep rules", rules_path=str(rules_path))
    try:
        result = subprocess.run(  # noqa: S603
            [
                ast_grep_bin,
                "scan",
                "--json=stream",
                "-r",
                str(rules_path),
                str(source.absolute()),
            ],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError as e:
        logger.debug(
            "Failed to run ast-grep rules",
            rules_path=str(rules_path),
            error=str(e),
        )
        return AstGrepMetrics(
            violations=[],
            total_violations=0,
            counts={},
            rules_checked=rules_checked,
        )
    if result.returncode != 0:
        logger.warning(
            "Failed to run ast-grep rules",
            rules_path=str(rules_path),
            error=result.stderr,
        )
        return AstGrepMetrics(
            violations=[],
            total_violations=0,
            counts={},
            rules_checked=rules_checked,
        )
    # Parse JSON stream output (one JSON object per line)
    for line in result.stdout.strip().split("\n"):
        if not line:
            continue
        try:
            match = json.loads(line)
            rule_id = match.get("ruleId", AST_GREP_CATEGORY)
            rule_info = rules_lookup.get(rule_id, {})
            violation = AstGrepViolation(
                rule_id=rule_id,
                severity=match.get("severity", "warning"),
                category=rule_info.get("category", AST_GREP_CATEGORY),
                subcategory=rule_info.get("subcategory", AST_GREP_CATEGORY),
                weight=rule_info.get("weight", 1),
                line=match["range"]["start"]["line"],
                column=match["range"]["start"]["column"],
                end_line=match["range"]["end"]["line"],
                end_column=match["range"]["end"]["column"],
            )
            violations.append(violation)
            counts[violation.rule_id] += 1
        except (json.JSONDecodeError, KeyError) as e:
            logger.warning(
                "Failed to parse ast-grep output",
                line=line,
                error=str(e),
            )
            continue

    return AstGrepMetrics(
        violations=violations,
        total_violations=len(violations),
        counts=dict(counts),
        rules_checked=rules_checked,
    )


def build_ast_grep_rules_lookup() -> RuleLookup:
    """Build lookup table mapping rule_id to category/subcategory/weight.

    Parses the configured slop rules file and extracts metadata for each rule.
    This is used for backfilling ast_grep.jsonl files with correct metadata.

    Returns:
        Dict mapping rule_id to {"category": str, "subcategory": str,
        "weight": int} where category is derived from the filename and
        subcategory from the rule's metadata.category field.
    """
    rules_path = _get_ast_grep_rules_path()
    if not rules_path.exists():
        logger.warning(
            "AST-grep rules file not found",
            rules_path=str(rules_path),
        )
        return {}

    lookup: RuleLookup = {}
    try:
        with rules_path.open(encoding="utf-8") as handle:
            for doc in yaml.safe_load_all(handle):
                if not doc:
                    continue
                rule_id = doc.get("id")
                if not rule_id:
                    continue
                metadata = doc.get("metadata", {})
                lookup[rule_id] = {
                    "category": AST_GREP_CATEGORY,
                    "subcategory": metadata.get("category", AST_GREP_CATEGORY),
                    "weight": metadata.get("weight", 1),
                }
    except (OSError, yaml.YAMLError) as exc:
        logger.warning(
            "Failed to parse AST-grep rules file for lookup",
            rules_path=str(rules_path),
            error=str(exc),
        )
        return {}

    logger.debug(
        "Built AST-grep rules lookup",
        rules_count=len(lookup),
    )
    return lookup
