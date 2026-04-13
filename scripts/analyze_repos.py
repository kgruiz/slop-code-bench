"""Analyze sampled git repo snapshots with static quality metrics.

This script reads a JSON manifest of remote repositories, samples
source-touching commits from each repository, checks those commits out into
isolated snapshot directories, and runs the existing static metrics pipeline on
each snapshot.
"""

from __future__ import annotations

import csv
import json
import random
import shutil
import subprocess
import tempfile
from contextlib import ExitStack
from dataclasses import asdict
from dataclasses import dataclass
from datetime import UTC
from datetime import datetime
from fnmatch import fnmatch
from pathlib import Path
from typing import Annotated
from urllib.parse import urlparse

import httpx
import typer
from git import GitCommandError
from git import Repo
from pydantic import BaseModel
from pydantic import ConfigDict
from pydantic import Field
from rich.console import Console

from slop_code.metrics import measure_snapshot_quality
from slop_code.metrics.checkpoint import compute_checkpoint_erosion
from slop_code.metrics.checkpoint import compute_checkpoint_verbosity
from slop_code.metrics.checkpoint import get_quality_metrics
from slop_code.metrics.languages.registry import EXT_TO_LANGUAGE
from slop_code.metrics.quality_io import save_quality_metrics

APP = typer.Typer(
    help="Analyze repo snapshots from a manifest of git remotes."
)
CONSOLE = Console()
DEFAULT_HTTP_TIMEOUT = 10.0
GIT_BIN = shutil.which("git") or "git"
DEFAULT_MANIFEST_PATH = (
    Path(__file__).resolve().parents[1] / "configs" / "repos" / "repos.json"
)


class RepoEntry(BaseModel):
    """One repository entry in the analysis manifest."""

    model_config = ConfigDict(extra="forbid")

    url: str
    name: str | None = None
    branch: str | None = None
    sample_count: int | None = Field(default=None, ge=1)
    include_globs: list[str] = Field(default_factory=list)
    exclude_globs: list[str] = Field(default_factory=list)
    entry_file: str | None = None
    pinned_refs: list[str] = Field(default_factory=list)


class RepoAnalysisManifest(BaseModel):
    """Config-first manifest for repo analysis."""

    model_config = ConfigDict(extra="forbid")

    repos: list[RepoEntry]
    default_sample_count: int = Field(default=5, ge=1)
    default_branch: str | None = None
    cache_dir: str | None = None
    output_dir: str | None = None
    seed: int = 42
    extensions: list[str] = Field(default_factory=list)


@dataclass(frozen=True)
class CommitCandidate:
    """A candidate commit for sampling."""

    sha: str
    committed_at: str


@dataclass
class CommitSummaryRow:
    """Summary row for one sampled commit or one repo-level failure."""

    repo_key: str
    repo_url: str
    branch: str | None
    stars: int | None
    commit_sha: str | None
    commit_short_sha: str | None
    commit_timestamp: str | None
    sample_ordinal: int | None
    resolved_entry_file: str | None
    snapshot_path: str | None
    file_count: int | None
    loc: int | None
    violation_pct: float | None
    ast_grep_violations: int | None
    ast_grep_rules_checked: int | None
    clone_ratio: float | None
    verbosity: float | None
    erosion: float | None
    status: str
    error: str | None = None


def load_manifest(manifest_path: Path) -> RepoAnalysisManifest:
    """Load a JSON manifest from disk."""

    with manifest_path.open() as manifest_file:
        return RepoAnalysisManifest.model_validate(json.load(manifest_file))


def get_supported_extensions(manifest: RepoAnalysisManifest) -> set[str]:
    """Return the extension set used for commit filtering and entry discovery."""

    if manifest.extensions:
        return {
            ext if ext.startswith(".") else f".{ext}"
            for ext in manifest.extensions
        }

    return set(EXT_TO_LANGUAGE)


def derive_repo_key(repo: RepoEntry) -> str:
    """Create a stable filesystem-safe repo key from a repo entry."""

    if repo.name:
        return sanitize_name(repo.name)

    if github_id := parse_github_repo(repo.url):
        owner, name = github_id
        return sanitize_name(f"{owner}-{name}")

    parsed = urlparse(repo.url)
    if parsed.path:
        raw = Path(parsed.path).stem or Path(parsed.path).name
        return sanitize_name(raw)

    return sanitize_name(repo.url)


def sanitize_name(value: str) -> str:
    """Normalize a string for filesystem use."""

    cleaned = []
    for char in value.strip():
        if char.isalnum() or char in {"-", "_", "."}:
            cleaned.append(char)
        else:
            cleaned.append("-")
    return "".join(cleaned).strip("-") or "repo"


def parse_github_repo(url: str) -> tuple[str, str] | None:
    """Extract the owner and repo name from a GitHub URL if possible."""

    if url.startswith("git@github.com:"):
        path = url.removeprefix("git@github.com:")
    else:
        parsed = urlparse(url)
        if parsed.netloc.lower() != "github.com":
            return None
        path = parsed.path.lstrip("/")

    parts = [part for part in path.split("/") if part]
    if len(parts) < 2:
        return None

    owner, repo_name = parts[0], parts[1]
    if repo_name.endswith(".git"):
        repo_name = repo_name[:-4]
    if not owner or not repo_name:
        return None
    return owner, repo_name


def fetch_github_stars(
    repo_url: str,
    client: httpx.Client | None = None,
) -> int | None:
    """Fetch current GitHub stars without requiring authentication."""

    parsed = parse_github_repo(repo_url)
    if parsed is None:
        return None

    owner, repo_name = parsed
    created_client = client is None
    if client is None:
        client = httpx.Client(timeout=DEFAULT_HTTP_TIMEOUT)

    try:
        response = client.get(
            f"https://api.github.com/repos/{owner}/{repo_name}",
            headers={"Accept": "application/vnd.github+json"},
        )
    except httpx.HTTPError:
        return None
    finally:
        if created_client:
            client.close()

    if response.status_code != 200:
        return None

    stars = response.json().get("stargazers_count")
    return stars if isinstance(stars, int) else None


def clone_or_update_repo(
    repo_url: str,
    cache_repo_path: Path,
    *,
    refresh: bool,
) -> Repo:
    """Clone a repo into the cache, or refresh it if already cached."""

    if cache_repo_path.exists():
        repo = Repo(cache_repo_path)
        if refresh:
            repo.remotes.origin.fetch(prune=True, tags=True)
        return repo

    cache_repo_path.parent.mkdir(parents=True, exist_ok=True)
    return Repo.clone_from(repo_url, cache_repo_path)


def resolve_branch_name(
    repo: Repo,
    repo_branch: str | None,
    default_branch: str | None,
) -> str:
    """Resolve the branch name used for commit sampling."""

    if repo_branch:
        return repo_branch

    if default_branch:
        return default_branch

    try:
        origin_head = repo.git.symbolic_ref("refs/remotes/origin/HEAD").strip()
        return origin_head.rsplit("/", maxsplit=1)[-1]
    except GitCommandError:
        pass

    try:
        return repo.active_branch.name
    except TypeError as error:
        raise RuntimeError(
            "Could not resolve a branch for repository sampling."
        ) from error


def get_commit_iter(repo: Repo, branch_name: str):
    """Return an iterator for commits on the resolved branch."""

    if branch_name in repo.heads:
        return repo.iter_commits(branch_name)

    remote_ref = f"origin/{branch_name}"
    if remote_ref in {ref.name for ref in repo.refs}:
        return repo.iter_commits(remote_ref)

    return repo.iter_commits(branch_name)


def path_matches_filters(
    path_str: str,
    extensions: set[str],
    include_globs: list[str],
    exclude_globs: list[str],
) -> bool:
    """Return whether a changed path counts as source-touching."""

    path = Path(path_str)
    if path.suffix not in extensions:
        return False

    posix_path = path.as_posix()

    if include_globs and not any(
        fnmatch(posix_path, pattern) for pattern in include_globs
    ):
        return False

    return not (
        exclude_globs
        and any(fnmatch(posix_path, pattern) for pattern in exclude_globs)
    )


def get_changed_paths(repo: Repo, commit_sha: str) -> list[str]:
    """Return changed paths for a commit."""

    output = repo.git.diff_tree(
        "--no-commit-id",
        "--name-only",
        "-r",
        "--root",
        commit_sha,
    )
    return [line.strip() for line in output.splitlines() if line.strip()]


def list_commit_candidates(
    repo: Repo,
    branch_name: str,
    extensions: set[str],
    include_globs: list[str],
    exclude_globs: list[str],
) -> list[CommitCandidate]:
    """Return source-touching commit candidates for a repo branch."""

    candidates: list[CommitCandidate] = []
    for commit in get_commit_iter(repo, branch_name):
        changed_paths = get_changed_paths(repo, commit.hexsha)
        if not any(
            path_matches_filters(
                path_str,
                extensions,
                include_globs,
                exclude_globs,
            )
            for path_str in changed_paths
        ):
            continue

        committed_at = commit.committed_datetime.astimezone(UTC).isoformat()
        candidates.append(
            CommitCandidate(sha=commit.hexsha, committed_at=committed_at)
        )

    return candidates


def select_commits(
    candidates: list[CommitCandidate],
    *,
    sample_count: int,
    seed: int,
    pinned_refs: list[str],
) -> list[CommitCandidate]:
    """Pick commit candidates using pinned refs or deterministic random sampling."""

    if pinned_refs:
        candidate_map = {candidate.sha: candidate for candidate in candidates}
        return [
            candidate_map[sha] for sha in pinned_refs if sha in candidate_map
        ]

    if len(candidates) <= sample_count:
        return sorted(candidates, key=lambda item: item.committed_at)

    randomizer = random.Random(seed)  # noqa: S311
    sampled = randomizer.sample(candidates, sample_count)
    return sorted(sampled, key=lambda item: item.committed_at)


def materialize_commit_snapshot(
    cache_repo_path: Path,
    commit_sha: str,
    snapshot_dir: Path,
) -> None:
    """Create a clean working snapshot for a single commit."""

    if snapshot_dir.exists():
        shutil.rmtree(snapshot_dir)

    subprocess.run(  # noqa: S603,S607
        [
            GIT_BIN,
            "clone",
            "--quiet",
            "--no-checkout",
            str(cache_repo_path),
            str(snapshot_dir),
        ],
        check=True,
    )
    subprocess.run(  # noqa: S603,S607
        [
            GIT_BIN,
            "-C",
            str(snapshot_dir),
            "checkout",
            "--quiet",
            "--detach",
            commit_sha,
        ],
        check=True,
    )
    git_dir = snapshot_dir / ".git"
    if git_dir.exists():
        shutil.rmtree(git_dir)


def discover_entry_file(
    snapshot_dir: Path,
    extensions: set[str],
    entry_file: str | None,
) -> Path:
    """Resolve the entry file for one snapshot."""

    if entry_file:
        explicit_path = snapshot_dir / entry_file
        if explicit_path.exists() and explicit_path.is_file():
            return explicit_path
        raise FileNotFoundError(f"Entry file '{entry_file}' not found.")

    matches = sorted(
        path
        for extension in sorted(extensions)
        for path in snapshot_dir.rglob(f"*{extension}")
        if path.is_file() and not path.name.startswith(".")
    )
    if not matches:
        raise FileNotFoundError("No supported source files found in snapshot.")
    return matches[0]


def run_metrics_for_snapshot(
    snapshot_dir: Path,
    entry_file: Path,
) -> dict[str, int | float]:
    """Run snapshot quality metrics and save the standard artifacts."""

    quality_result, file_metrics = measure_snapshot_quality(entry_file, snapshot_dir)
    save_quality_metrics(snapshot_dir, quality_result, file_metrics)

    flat_metrics = get_quality_metrics(snapshot_dir)
    verbosity = compute_checkpoint_verbosity(flat_metrics)
    erosion = compute_checkpoint_erosion(flat_metrics)

    return {
        "file_count": quality_result.file_count,
        "loc": int(flat_metrics.get("loc", 0)),
        "violation_pct": float(flat_metrics.get("violation_pct", 0.0)),
        "ast_grep_violations": quality_result.ast_grep.violations,
        "ast_grep_rules_checked": quality_result.ast_grep.rules_checked,
        "clone_ratio": float(flat_metrics.get("cloned_pct", 0.0)),
        "verbosity": float(verbosity) if verbosity is not None else 0.0,
        "erosion": float(erosion) if erosion is not None else 0.0,
    }


def write_summary_csv(output_path: Path, rows: list[CommitSummaryRow]) -> None:
    """Write per-commit summary rows to CSV."""

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", newline="") as csv_file:
        writer = csv.DictWriter(csv_file, fieldnames=list(asdict(rows[0]).keys()))
        writer.writeheader()
        for row in rows:
            writer.writerow(asdict(row))


def write_summary_json(
    output_path: Path,
    rows: list[CommitSummaryRow],
    manifest_path: Path,
) -> None:
    """Write machine-readable run summary."""

    output_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "manifest": str(manifest_path),
        "generated_at": datetime.now(tz=UTC).isoformat(),
        "rows": [asdict(row) for row in rows],
    }
    with output_path.open("w") as summary_file:
        json.dump(payload, summary_file, indent=2, sort_keys=True)


def analyze_manifest(
    manifest_path: Path,
    *,
    output_dir: Path | None = None,
    seed: int | None = None,
    no_cache: bool = False,
    refresh: bool = False,
    repo_name: str | None = None,
) -> list[CommitSummaryRow]:
    """Run repo analysis for a manifest and return all summary rows."""

    manifest = load_manifest(manifest_path)
    extensions = get_supported_extensions(manifest)

    effective_output_dir = (
        output_dir
        if output_dir is not None
        else Path(manifest.output_dir or "outputs/repo-analysis")
    )
    effective_output_dir.mkdir(parents=True, exist_ok=True)

    cache_dir = Path(manifest.cache_dir or ".cache/repo-analysis")
    rows: list[CommitSummaryRow] = []

    repos = manifest.repos
    if repo_name is not None:
        repos = [
            repo for repo in repos if (repo.name or derive_repo_key(repo)) == repo_name
        ]

    with ExitStack() as exit_stack:
        http_client = exit_stack.enter_context(
            httpx.Client(timeout=DEFAULT_HTTP_TIMEOUT)
        )

        for repo in repos:
            repo_key = derive_repo_key(repo)
            repo_seed = seed if seed is not None else manifest.seed
            sample_count = repo.sample_count or manifest.default_sample_count
            repo_output_dir = effective_output_dir / repo_key
            repo_output_dir.mkdir(parents=True, exist_ok=True)

            stars = fetch_github_stars(repo.url, client=http_client)
            if parse_github_repo(repo.url) and stars is None:
                CONSOLE.print(
                    f"[yellow]{repo_key}: could not fetch GitHub stars; continuing.[/yellow]"
                )

            try:
                if no_cache:
                    temp_dir = Path(exit_stack.enter_context(tempfile.TemporaryDirectory()))
                    cache_repo_path = temp_dir / repo_key
                else:
                    cache_repo_path = cache_dir / repo_key

                cached_repo = clone_or_update_repo(
                    repo.url,
                    cache_repo_path,
                    refresh=refresh,
                )
                branch_name = resolve_branch_name(
                    cached_repo,
                    repo.branch,
                    manifest.default_branch,
                )
                candidates = list_commit_candidates(
                    cached_repo,
                    branch_name,
                    extensions,
                    repo.include_globs,
                    repo.exclude_globs,
                )
                selected = select_commits(
                    candidates,
                    sample_count=sample_count,
                    seed=repo_seed,
                    pinned_refs=repo.pinned_refs,
                )
            except Exception as error:  # noqa: BLE001
                rows.append(
                    CommitSummaryRow(
                        repo_key=repo_key,
                        repo_url=repo.url,
                        branch=repo.branch or manifest.default_branch,
                        stars=stars,
                        commit_sha=None,
                        commit_short_sha=None,
                        commit_timestamp=None,
                        sample_ordinal=None,
                        resolved_entry_file=None,
                        snapshot_path=None,
                        file_count=None,
                        loc=None,
                        violation_pct=None,
                        ast_grep_violations=None,
                        ast_grep_rules_checked=None,
                        clone_ratio=None,
                        verbosity=None,
                        erosion=None,
                        status="repo_failed",
                        error=str(error),
                    )
                )
                continue

            if not selected:
                rows.append(
                    CommitSummaryRow(
                        repo_key=repo_key,
                        repo_url=repo.url,
                        branch=branch_name,
                        stars=stars,
                        commit_sha=None,
                        commit_short_sha=None,
                        commit_timestamp=None,
                        sample_ordinal=None,
                        resolved_entry_file=None,
                        snapshot_path=None,
                        file_count=None,
                        loc=None,
                        violation_pct=None,
                        ast_grep_violations=None,
                        ast_grep_rules_checked=None,
                        clone_ratio=None,
                        verbosity=None,
                        erosion=None,
                        status="skipped",
                        error="No eligible source-touching commits found.",
                    )
                )
                continue

            for index, candidate in enumerate(selected, start=1):
                snapshot_dir = (
                    repo_output_dir / f"{index:03d}-{candidate.sha[:8]}"
                )
                try:
                    materialize_commit_snapshot(
                        cache_repo_path,
                        candidate.sha,
                        snapshot_dir,
                    )
                    entry_path = discover_entry_file(
                        snapshot_dir,
                        extensions,
                        repo.entry_file,
                    )
                    metric_summary = run_metrics_for_snapshot(
                        snapshot_dir,
                        entry_path,
                    )
                    rows.append(
                        CommitSummaryRow(
                            repo_key=repo_key,
                            repo_url=repo.url,
                            branch=branch_name,
                            stars=stars,
                            commit_sha=candidate.sha,
                            commit_short_sha=candidate.sha[:8],
                            commit_timestamp=candidate.committed_at,
                            sample_ordinal=index,
                            resolved_entry_file=str(
                                entry_path.relative_to(snapshot_dir)
                            ),
                            snapshot_path=str(snapshot_dir),
                            file_count=int(metric_summary["file_count"]),
                            loc=int(metric_summary["loc"]),
                            violation_pct=float(metric_summary["violation_pct"]),
                            ast_grep_violations=int(
                                metric_summary["ast_grep_violations"]
                            ),
                            ast_grep_rules_checked=int(
                                metric_summary["ast_grep_rules_checked"]
                            ),
                            clone_ratio=float(metric_summary["clone_ratio"]),
                            verbosity=float(metric_summary["verbosity"]),
                            erosion=float(metric_summary["erosion"]),
                            status="ok",
                            error=None,
                        )
                    )
                except Exception as error:  # noqa: BLE001
                    rows.append(
                        CommitSummaryRow(
                            repo_key=repo_key,
                            repo_url=repo.url,
                            branch=branch_name,
                            stars=stars,
                            commit_sha=candidate.sha,
                            commit_short_sha=candidate.sha[:8],
                            commit_timestamp=candidate.committed_at,
                            sample_ordinal=index,
                            resolved_entry_file=None,
                            snapshot_path=str(snapshot_dir),
                            file_count=None,
                            loc=None,
                            violation_pct=None,
                            ast_grep_violations=None,
                            ast_grep_rules_checked=None,
                            clone_ratio=None,
                            verbosity=None,
                            erosion=None,
                            status="failed",
                            error=str(error),
                        )
                    )

    if rows:
        write_summary_json(
            effective_output_dir / "summary.json",
            rows,
            manifest_path,
        )
        write_summary_csv(effective_output_dir / "summary.csv", rows)

    return rows


@APP.command()
def main(
    manifest: Annotated[
        Path,
        typer.Option(
            DEFAULT_MANIFEST_PATH,
            exists=True,
            dir_okay=False,
            help="Path to the repo analysis manifest JSON file.",
        ),
    ] = DEFAULT_MANIFEST_PATH,
    output_dir: Annotated[Path | None, typer.Option()] = None,
    seed: Annotated[int | None, typer.Option()] = None,
    no_cache: Annotated[
        bool | None, typer.Option("--no-cache", is_flag=True)
    ] = None,
    refresh: Annotated[
        bool | None, typer.Option("--refresh", is_flag=True)
    ] = None,
    repo: Annotated[str | None, typer.Option()] = None,
) -> None:
    """Run manifest-driven repo snapshot analysis."""

    rows = analyze_manifest(
        manifest,
        output_dir=output_dir,
        seed=seed,
        no_cache=bool(no_cache),
        refresh=bool(refresh),
        repo_name=repo,
    )

    ok_count = sum(1 for row in rows if row.status == "ok")
    fail_count = sum(1 for row in rows if row.status not in {"ok", "skipped"})
    skip_count = sum(1 for row in rows if row.status == "skipped")
    CONSOLE.print(
        f"Analyzed {ok_count} snapshots "
        f"({fail_count} failed, {skip_count} skipped)."
    )
    if ok_count == 0:
        raise typer.Exit(1)


if __name__ == "__main__":
    APP()
