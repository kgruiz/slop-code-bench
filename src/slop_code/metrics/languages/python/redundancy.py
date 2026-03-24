"""Code clone detection via AST hashing.

Clones are measured in lines.  When clone groups overlap (e.g. a cloned
function contains a cloned if-block), lines are deduplicated via a
line-number union so no line is counted twice.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import TYPE_CHECKING

from slop_code.metrics.languages.python.constants import CLONE_NODE_TYPES
from slop_code.metrics.languages.python.line_metrics import (
    calculate_line_metrics,
)
from slop_code.metrics.languages.python.parser import get_python_parser
from slop_code.metrics.languages.python.utils import read_python_code
from slop_code.metrics.models import CodeClone
from slop_code.metrics.models import RedundancyMetrics

if TYPE_CHECKING:
    from tree_sitter import Node


def _normalize_ast(node: Node) -> str:
    """Create normalized string representation of AST subtree."""
    var_map: dict[str, str] = {}
    var_counter = 0
    literal_tokens = {
        "string": "$STR",
        "string_content": "$STR",
        "f_string": "$STR",
        "string_fragment": "$STR",
        "bytes": "$STR",
        "integer": "$INT",
        "float": "$FLOAT",
        "imaginary": "$FLOAT",
        "true": "$BOOL",
        "false": "$BOOL",
        "none": "$NONE",
    }

    def normalize(n: Node) -> str:
        nonlocal var_counter
        if n.type == "identifier":
            name = n.text.decode("utf-8")
            if name not in var_map:
                var_counter += 1
                var_map[name] = f"$VAR{var_counter}"
            return var_map[name]

        if n.type in literal_tokens:
            return literal_tokens[n.type]

        if not n.named_children:
            return n.type

        child_parts = [normalize(child) for child in n.named_children]
        return f"{n.type}({','.join(child_parts)})"

    return normalize(node)


def _hash_ast_subtree(node: Node) -> str:
    """Generate hash of normalized AST subtree."""
    normalized = _normalize_ast(node)
    return hashlib.md5(normalized.encode()).hexdigest()[:12]


def detect_code_clones(source: Path, min_lines: int = 3) -> RedundancyMetrics:
    """Detect duplicate code blocks via AST hashing."""
    code = read_python_code(source)
    if not code.strip():
        return RedundancyMetrics(
            clones=[], total_clone_instances=0, clone_lines=0, clone_ratio=0.0
        )

    parser = get_python_parser()
    tree = parser.parse(code.encode("utf-8"))
    source_lines = code.splitlines()

    groups: dict[str, list[tuple[Node, str, int]]] = {}
    stack = [tree.root_node]

    while stack:
        current = stack.pop()
        if current.type in CLONE_NODE_TYPES:
            line_count = current.end_point[0] - current.start_point[0] + 1
            if line_count >= min_lines:
                ast_hash = _hash_ast_subtree(current)
                groups.setdefault(ast_hash, []).append(
                    (current, current.type, line_count)
                )
        stack.extend(current.children)

    clones: list[CodeClone] = []
    total_instances = 0
    clone_line_set: set[int] = set()
    for ast_hash, nodes in groups.items():
        if len(nodes) < 2:
            continue
        locations = [
            (n.start_point[0] + 1, n.end_point[0] + 1) for n, _, _ in nodes
        ]
        node_type = nodes[0][1]
        line_count = nodes[0][2]
        clones.append(
            CodeClone(
                ast_hash=ast_hash,
                locations=locations,
                node_type=node_type,
                line_count=line_count,
            )
        )
        total_instances += len(nodes)
        for n, _, _ in nodes:
            clone_line_set.update(range(n.start_point[0], n.end_point[0] + 1))

    total_lines = calculate_line_metrics(source).loc
    num_clone_lines = len(clone_line_set)
    clone_sloc_lines = sum(
        1
        for line_number in clone_line_set
        if line_number < len(source_lines)
        and (stripped := source_lines[line_number].strip())
        and not stripped.startswith("#")
    )
    clone_ratio = (clone_sloc_lines / total_lines) if total_lines else 0.0

    return RedundancyMetrics(
        clones=clones,
        total_clone_instances=total_instances,
        clone_lines=num_clone_lines,
        clone_ratio=clone_ratio,
    )


def calculate_redundancy_metrics(source: Path) -> RedundancyMetrics:
    """Calculate redundancy metrics for a Python file."""
    return detect_code_clones(source)
