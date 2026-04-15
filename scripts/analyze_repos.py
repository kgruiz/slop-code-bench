"""Analyze sampled git repo snapshots with static quality metrics.

This script reads a JSON manifest of remote repositories, samples
source-touching commits from each repository, checks those commits out into
isolated snapshot directories, and runs the existing static metrics pipeline on
each snapshot.
"""
# ruff: noqa: E402, I001

from __future__ import annotations

import csv
import json
import random
import shutil
import subprocess
import sys
import tempfile
import warnings
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

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = PROJECT_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from slop_code.metrics import measure_snapshot_quality
from slop_code.metrics.checkpoint import compute_checkpoint_erosion
from slop_code.metrics.checkpoint import compute_checkpoint_verbosity
from slop_code.metrics.checkpoint import get_quality_metrics
from slop_code.metrics.languages.python.ast_grep import _get_ast_grep_binary
from slop_code.metrics.languages.registry import EXT_TO_LANGUAGE
from slop_code.metrics.quality_io import save_quality_metrics

APP = typer.Typer(
    help="Analyze repo snapshots from a manifest of git remotes."
)
CONSOLE = Console()
DEFAULT_HTTP_TIMEOUT = 10.0
GIT_BIN = shutil.which("git") or "git"
DEFAULT_MANIFEST_PATH = (
    PROJECT_ROOT / "configs" / "repos" / "repos.json"
)


def get_progress_timestamp() -> str:
    """Return the current wall-clock time for progress output."""

    return datetime.now().strftime("%H:%M:%S")


def print_progress(message: str) -> None:
    """Print one timestamped progress line."""

    CONSOLE.print(f"[dim]{get_progress_timestamp()}[/dim] {message}")


class RepoEntry(BaseModel):
    """One repository entry in the analysis manifest."""

    model_config = ConfigDict(extra="forbid")

    url: str | None
    name: str | None = None
    stars: str | None = None
    note: str | None = None
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
    repo_url: str | None
    branch: str | None
    stars: str | None
    note: str | None
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

    if repo.url and (github_id := parse_github_repo(repo.url)):
        owner, name = github_id
        return sanitize_name(f"{owner}-{name}")

    if repo.url:
        parsed = urlparse(repo.url)
        if parsed.path:
            raw = Path(parsed.path).stem or Path(parsed.path).name
            return sanitize_name(raw)

    return sanitize_name(repo.name or "repo")


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


def canonical_repo_identity(repo_url: str) -> str:
    """Return a stable identity string for cache-collision checks."""

    if github_id := parse_github_repo(repo_url):
        owner, repo_name = github_id
        return f"github:{owner.lower()}/{repo_name.lower()}"

    parsed = urlparse(repo_url)
    if parsed.scheme and parsed.netloc:
        path = parsed.path.rstrip("/")
        if path.endswith(".git"):
            path = path[:-4]
        return f"url:{parsed.scheme.lower()}://{parsed.netloc.lower()}{path}"

    path = Path(repo_url).expanduser()
    try:
        return f"path:{path.resolve()}"
    except OSError:
        return f"path:{path}"


def validate_cache_repo_targets(
    repos: list[RepoEntry],
    cache_dir: Path,
) -> None:
    """Fail if different repos would reuse the same persistent cache path."""

    cache_targets: dict[Path, str] = {}
    for repo in repos:
        if not repo.url:
            continue

        repo_key = derive_repo_key(repo)
        cache_repo_path = cache_dir / repo_key
        repo_identity = canonical_repo_identity(repo.url)
        existing_identity = cache_targets.get(cache_repo_path)
        if existing_identity is None:
            cache_targets[cache_repo_path] = repo_identity
            continue

        if existing_identity != repo_identity:
            raise ValueError(
                "Cache directory collision detected: "
                f"{cache_repo_path} would be reused for different repos "
                f"({existing_identity} vs {repo_identity}). "
                "Set distinct manifest names to avoid the collision."
            )


def fetch_github_stars(
    repo_url: str,
    client: httpx.Client | None = None,
) -> str | None:
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
    return str(stars) if isinstance(stars, int) else None


def clone_or_update_repo(
    repo_url: str,
    cache_repo_path: Path,
    *,
    refresh: bool,
) -> tuple[Repo, str]:
    """Clone a repo into the cache, or refresh it if already cached."""

    if cache_repo_path.exists():
        repo = Repo(cache_repo_path)
        if refresh:
            repo.remotes.origin.fetch(prune=True, tags=True)
            return repo, "refreshed"
        return repo, "reused"

    cache_repo_path.parent.mkdir(parents=True, exist_ok=True)
    return Repo.clone_from(repo_url, cache_repo_path), "cloned"


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
    artifact_dir: Path,
    *,
    debug: bool = False,
    repo_label: str | None = None,
    commit_label: str | None = None,
) -> dict[str, int | float]:
    """Run snapshot quality metrics and save the standard artifacts."""

    relative_entry_file = entry_file.relative_to(snapshot_dir)
    if debug and repo_label and commit_label:
        print_progress(
            f"[blue]{repo_label}[/blue]: {commit_label}: running "
            "[bold]measure_snapshot_quality[/bold]"
        )
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", SyntaxWarning)
        quality_result, file_metrics = measure_snapshot_quality(
            relative_entry_file, snapshot_dir
        )
    if debug and repo_label and commit_label:
        print_progress(
            f"[blue]{repo_label}[/blue]: {commit_label}: writing "
            "[bold]quality_analysis[/bold] artifacts"
        )
    save_quality_metrics(artifact_dir, quality_result, file_metrics)

    if debug and repo_label and commit_label:
        print_progress(
            f"[blue]{repo_label}[/blue]: {commit_label}: computing "
            "[bold]loc[/bold], [bold]violation_pct[/bold], "
            "[bold]clone_ratio[/bold], [bold]verbosity[/bold], "
            "[bold]erosion[/bold]"
        )
    flat_metrics = get_quality_metrics(artifact_dir)
    verbosity = compute_checkpoint_verbosity(flat_metrics)
    erosion = compute_checkpoint_erosion(flat_metrics)

    metric_summary = {
        "file_count": quality_result.file_count,
        "loc": int(flat_metrics.get("loc", 0)),
        "violation_pct": float(flat_metrics.get("violation_pct", 0.0)),
        "ast_grep_violations": quality_result.ast_grep.violations,
        "ast_grep_rules_checked": quality_result.ast_grep.rules_checked,
        "clone_ratio": float(flat_metrics.get("cloned_pct", 0.0)),
        "verbosity": float(verbosity) if verbosity is not None else 0.0,
        "erosion": float(erosion) if erosion is not None else 0.0,
    }
    if debug and repo_label and commit_label:
        print_progress(
            f"[blue]{repo_label}[/blue]: {commit_label}: metrics "
            f"LOC=[bold]{metric_summary['loc']}[/bold] "
            f"files=[bold]{metric_summary['file_count']}[/bold] "
            f"violation_pct=[bold]{metric_summary['violation_pct']:.4f}[/bold] "
            f"clone_ratio=[bold]{metric_summary['clone_ratio']:.4f}[/bold] "
            f"verbosity=[bold]{metric_summary['verbosity']:.4f}[/bold] "
            f"erosion=[bold]{metric_summary['erosion']:.4f}[/bold]"
        )
    return metric_summary


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
    skip_repos: tuple[str, ...] = (),
    debug: bool = False,
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
    if skip_repos:
        skip_repo_names = set(skip_repos)
        repos = [
            repo
            for repo in repos
            if (repo.name or derive_repo_key(repo)) not in skip_repo_names
        ]

    if not no_cache:
        validate_cache_repo_targets(repos, cache_dir)

    ast_grep_bin = _get_ast_grep_binary()
    if ast_grep_bin is None:
        print_progress(
            "[yellow]ast-grep[/yellow]: not found; AST-grep rule checks will be skipped"
        )
    else:
        print_progress(
            f"[cyan]ast-grep[/cyan]: using [bold]{ast_grep_bin}[/bold]"
        )

    with ExitStack() as exit_stack:
        http_client = exit_stack.enter_context(
            httpx.Client(timeout=DEFAULT_HTTP_TIMEOUT)
        )
        workspace_root = Path(
            exit_stack.enter_context(tempfile.TemporaryDirectory())
        )

        total_repos = len(repos)
        for repo_index, repo in enumerate(repos, start=1):
            repo_key = derive_repo_key(repo)
            repo_label = f"[{repo_index}/{total_repos}] {repo_key}"
            repo_seed = seed if seed is not None else manifest.seed
            sample_count = repo.sample_count or manifest.default_sample_count
            repo_output_dir = effective_output_dir / repo_key
            stars = repo.stars
            if repo.url is None:
                print_progress(
                    f"[yellow]{repo_label}[/yellow]: no URL configured, skipping"
                )
                rows.append(
                    CommitSummaryRow(
                        repo_key=repo_key,
                        repo_url=None,
                        branch=repo.branch or manifest.default_branch,
                        stars=stars,
                        note=repo.note,
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
                        error="No URL configured.",
                    )
                )
                continue

            repo_output_dir.mkdir(parents=True, exist_ok=True)
            print_progress(f"[cyan]{repo_label}[/cyan]: preparing repository")

            if stars is None:
                stars = fetch_github_stars(repo.url, client=http_client)
                if parse_github_repo(repo.url) and stars is None:
                    print_progress(
                        f"[yellow]{repo_label}: could not fetch GitHub stars; continuing.[/yellow]"
                    )

            try:
                if no_cache:
                    temp_dir = Path(exit_stack.enter_context(tempfile.TemporaryDirectory()))
                    cache_repo_path = temp_dir / repo_key
                    print_progress(
                        f"[cyan]{repo_label}[/cyan]: cloning temporary repository"
                    )
                else:
                    cache_repo_path = cache_dir / repo_key
                    if cache_repo_path.exists():
                        if refresh:
                            print_progress(
                                f"[cyan]{repo_label}[/cyan]: refreshing cached repository"
                            )
                        else:
                            print_progress(
                                f"[cyan]{repo_label}[/cyan]: reusing cached repository"
                            )
                    else:
                        print_progress(
                            f"[cyan]{repo_label}[/cyan]: cloning repository into cache"
                        )

                cached_repo, repo_state = clone_or_update_repo(
                    repo.url,
                    cache_repo_path,
                    refresh=refresh,
                )
                if repo_state == "cloned":
                    print_progress(
                        f"[cyan]{repo_label}[/cyan]: clone complete"
                    )
                elif repo_state == "refreshed":
                    print_progress(
                        f"[cyan]{repo_label}[/cyan]: cache refresh complete"
                    )
                branch_name = resolve_branch_name(
                    cached_repo,
                    repo.branch,
                    manifest.default_branch,
                )
                print_progress(
                    f"[cyan]{repo_label}[/cyan]: scanning commit history on branch "
                    f"[bold]{branch_name}[/bold]"
                )
                candidates = list_commit_candidates(
                    cached_repo,
                    branch_name,
                    extensions,
                    repo.include_globs,
                    repo.exclude_globs,
                )
                print_progress(
                    f"[cyan]{repo_label}[/cyan]: found {len(candidates)} eligible commit(s)"
                )
                selected = select_commits(
                    candidates,
                    sample_count=sample_count,
                    seed=repo_seed,
                    pinned_refs=repo.pinned_refs,
                )
                if repo.pinned_refs:
                    print_progress(
                        f"[cyan]{repo_label}[/cyan]: using {len(selected)} pinned ref(s)"
                    )
                else:
                    print_progress(
                        f"[cyan]{repo_label}[/cyan]: selected {len(selected)} commit(s) with seed "
                        f"[bold]{repo_seed}[/bold]"
                    )

                stars_text = f" stars=[bold]{stars}[/bold]" if stars else ""
                print_progress(
                    f"[cyan]{repo_label}[/cyan]: ready on branch [bold]{branch_name}[/bold]{stars_text}"
                )
            except Exception as error:  # noqa: BLE001
                print_progress(
                    f"[red]{repo_label}[/red]: repository setup failed: {error}"
                )
                rows.append(
                    CommitSummaryRow(
                        repo_key=repo_key,
                        repo_url=repo.url,
                        branch=repo.branch or manifest.default_branch,
                        stars=stars,
                        note=repo.note,
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
                print_progress(
                    f"[yellow]{repo_label}[/yellow]: no eligible source-touching commits found"
                )
                rows.append(
                    CommitSummaryRow(
                        repo_key=repo_key,
                        repo_url=repo.url,
                        branch=branch_name,
                        stars=stars,
                        note=repo.note,
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

            print_progress(
                f"[cyan]{repo_label}[/cyan]: analyzing {len(selected)} commit(s) from branch "
                f"[bold]{branch_name}[/bold]"
            )
            for index, candidate in enumerate(selected, start=1):
                artifact_dir = repo_output_dir / f"{index:03d}-{candidate.sha[:8]}"
                snapshot_dir = (
                    workspace_root
                    / repo_key
                    / f"{index:03d}-{candidate.sha[:8]}"
                )
                commit_label = (
                    f"commit {index}/{len(selected)} "
                    f"[bold]{candidate.sha[:8]}[/bold]"
                )
                print_progress(
                    f"[cyan]{repo_label}[/cyan]: {commit_label}"
                )
                try:
                    if debug:
                        print_progress(
                            f"[blue]{repo_label}[/blue]: {commit_label}: materializing snapshot"
                        )
                    materialize_commit_snapshot(
                        cache_repo_path,
                        candidate.sha,
                        snapshot_dir,
                    )
                    if debug:
                        print_progress(
                            f"[blue]{repo_label}[/blue]: {commit_label}: discovering entry file"
                        )
                    entry_path = discover_entry_file(
                        snapshot_dir,
                        extensions,
                        repo.entry_file,
                    )
                    if debug:
                        print_progress(
                            f"[blue]{repo_label}[/blue]: {commit_label}: entry file "
                            f"[bold]{entry_path.relative_to(snapshot_dir)}[/bold]"
                        )
                    metric_kwargs: dict[str, object] = {}
                    if debug:
                        metric_kwargs = {
                            "debug": True,
                            "repo_label": repo_label,
                            "commit_label": commit_label,
                        }
                    metric_summary = run_metrics_for_snapshot(
                        snapshot_dir,
                        entry_path,
                        artifact_dir,
                        **metric_kwargs,
                    )
                    row = CommitSummaryRow(
                        repo_key=repo_key,
                        repo_url=repo.url,
                        branch=branch_name,
                        stars=stars,
                        note=repo.note,
                        commit_sha=candidate.sha,
                        commit_short_sha=candidate.sha[:8],
                        commit_timestamp=candidate.committed_at,
                        sample_ordinal=index,
                        resolved_entry_file=str(
                            entry_path.relative_to(snapshot_dir)
                        ),
                        snapshot_path=str(artifact_dir),
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
                    rows.append(row)
                    print_progress(
                        f"[green]{repo_label}[/green]: completed {candidate.sha[:8]} "
                        f"at [bold]{candidate.committed_at}[/bold] "
                        f"LOC=[bold]{row.loc}[/bold] "
                        f"files=[bold]{row.file_count}[/bold] -> {artifact_dir}"
                    )
                except Exception as error:  # noqa: BLE001
                    print_progress(
                        f"[red]{repo_label}[/red]: failed {candidate.sha[:8]}: {error}"
                    )
                    rows.append(
                        CommitSummaryRow(
                            repo_key=repo_key,
                            repo_url=repo.url,
                            branch=branch_name,
                            stars=stars,
                            note=repo.note,
                            commit_sha=candidate.sha,
                            commit_short_sha=candidate.sha[:8],
                            commit_timestamp=candidate.committed_at,
                            sample_ordinal=index,
                            resolved_entry_file=None,
                            snapshot_path=str(artifact_dir),
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
            "--manifest",
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
    skip_repo: Annotated[
        list[str] | None,
        typer.Option(
            "--skip-repo",
            help="Repo name/key to exclude. May be provided multiple times.",
        ),
    ] = None,
    debug: Annotated[
        bool | None,
        typer.Option(
            "--debug",
            is_flag=True,
            help="Print per-commit stage and metric details during analysis.",
        ),
    ] = None,
) -> None:
    """Run manifest-driven repo snapshot analysis."""

    rows = analyze_manifest(
        manifest,
        output_dir=output_dir,
        seed=seed,
        no_cache=bool(no_cache),
        refresh=bool(refresh),
        repo_name=repo,
        skip_repos=tuple(skip_repo or ()),
        debug=bool(debug),
    )

    ok_count = sum(1 for row in rows if row.status == "ok")
    fail_count = sum(1 for row in rows if row.status not in {"ok", "skipped"})
    skip_count = sum(1 for row in rows if row.status == "skipped")
    print_progress(
        f"Analyzed {ok_count} snapshots "
        f"({fail_count} failed, {skip_count} skipped)."
    )
    if ok_count == 0:
        raise typer.Exit(1)


if __name__ == "__main__":
    APP()
