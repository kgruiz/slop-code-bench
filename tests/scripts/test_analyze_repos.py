from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import httpx
import pytest

MODULE_PATH = (
    Path(__file__).resolve().parents[2] / "scripts" / "analyze_repos.py"
)
SPEC = importlib.util.spec_from_file_location("analyze_repos_module", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


def _run_git(*args: str, cwd: Path) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True)  # noqa: S603,S607


def _write_json(path: Path, data: dict) -> None:
    path.write_text(json.dumps(data, indent=2))


@pytest.fixture
def git_remote(tmp_path: Path) -> Path:
    remote_dir = tmp_path / "remote.git"
    work_dir = tmp_path / "work"

    _run_git("init", "--bare", str(remote_dir), cwd=tmp_path)
    _run_git("init", str(work_dir), cwd=tmp_path)
    _run_git("config", "user.email", "test@example.com", cwd=work_dir)
    _run_git("config", "user.name", "Test User", cwd=work_dir)

    (work_dir / "README.md").write_text("notes\n")
    _run_git("add", "README.md", cwd=work_dir)
    _run_git("commit", "-m", "docs", cwd=work_dir)

    (work_dir / "app.py").write_text("print('one')\n")
    _run_git("add", "app.py", cwd=work_dir)
    _run_git("commit", "-m", "python one", cwd=work_dir)

    (work_dir / "notes.txt").write_text("ignore me\n")
    _run_git("add", "notes.txt", cwd=work_dir)
    _run_git("commit", "-m", "notes", cwd=work_dir)

    (work_dir / "pkg").mkdir()
    (work_dir / "pkg" / "core.py").write_text("def core():\n    return 2\n")
    _run_git("add", "pkg/core.py", cwd=work_dir)
    _run_git("commit", "-m", "python two", cwd=work_dir)

    _run_git("remote", "add", "origin", str(remote_dir), cwd=work_dir)
    _run_git("branch", "-M", "main", cwd=work_dir)
    _run_git("push", "-u", "origin", "main", cwd=work_dir)

    return remote_dir


def test_load_manifest_parses_repo_defaults(tmp_path: Path):
    manifest_path = tmp_path / "repos.json"
    _write_json(
        manifest_path,
        {
            "default_sample_count": 3,
            "seed": 99,
            "repos": [{"url": "https://github.com/org/repo.git"}],
        },
    )

    manifest = MODULE.load_manifest(manifest_path)

    assert manifest.default_sample_count == 3
    assert manifest.seed == 99
    assert manifest.repos[0].url == "https://github.com/org/repo.git"


def test_load_manifest_accepts_stars_note_and_null_url(tmp_path: Path):
    manifest_path = tmp_path / "repos.json"
    _write_json(
        manifest_path,
        {
            "repos": [
                {
                    "name": "python-projects",
                    "stars": "2.1k",
                    "url": None,
                    "note": "multiple repos with this name",
                }
            ]
        },
    )

    manifest = MODULE.load_manifest(manifest_path)

    assert manifest.repos[0].name == "python-projects"
    assert manifest.repos[0].stars == "2.1k"
    assert manifest.repos[0].url is None
    assert manifest.repos[0].note == "multiple repos with this name"


def test_parse_github_repo_supports_https_and_ssh():
    assert MODULE.parse_github_repo("https://github.com/openai/codex.git") == (
        "openai",
        "codex",
    )
    assert MODULE.parse_github_repo("git@github.com:openai/codex.git") == (
        "openai",
        "codex",
    )
    assert MODULE.parse_github_repo("https://gitlab.com/openai/codex.git") is None


def test_fetch_github_stars_returns_none_on_http_failure(monkeypatch: pytest.MonkeyPatch):
    class FakeClient:
        def get(self, *args, **kwargs):
            raise httpx.ConnectError("boom")

    stars = MODULE.fetch_github_stars(
        "https://github.com/openai/codex",
        client=FakeClient(),
    )

    assert stars is None


def test_validate_cache_repo_targets_allows_same_repo_same_cache_path(tmp_path: Path):
    repos = [
        MODULE.RepoEntry(
            name="same",
            url="https://github.com/psf/requests.git",
        ),
        MODULE.RepoEntry(
            name="same",
            url="git@github.com:psf/requests.git",
        ),
    ]

    MODULE.validate_cache_repo_targets(repos, tmp_path / "cache")


def test_validate_cache_repo_targets_rejects_different_repo_collision(tmp_path: Path):
    repos = [
        MODULE.RepoEntry(
            name="same",
            url="https://github.com/psf/requests.git",
        ),
        MODULE.RepoEntry(
            name="same",
            url="https://github.com/pallets/flask.git",
        ),
    ]

    with pytest.raises(ValueError, match="Cache directory collision detected"):
        MODULE.validate_cache_repo_targets(repos, tmp_path / "cache")


def test_validate_cache_repo_targets_ignores_missing_urls(tmp_path: Path):
    repos = [
        MODULE.RepoEntry(
            name="missing",
            url=None,
            stars="2.1k",
            note="missing URL",
        ),
        MODULE.RepoEntry(
            name="same",
            url="https://github.com/psf/requests.git",
        ),
        MODULE.RepoEntry(
            name="same",
            url="git@github.com:psf/requests.git",
        ),
    ]

    MODULE.validate_cache_repo_targets(repos, tmp_path / "cache")


def test_list_commit_candidates_only_includes_source_touching(git_remote: Path, tmp_path: Path):
    cache_dir = tmp_path / "cache"
    repo, _repo_state = MODULE.clone_or_update_repo(
        str(git_remote),
        cache_dir / "sample",
        refresh=False,
    )
    branch = MODULE.resolve_branch_name(repo, None, None)

    candidates = MODULE.list_commit_candidates(
        repo,
        branch,
        {".py"},
        [],
        [],
    )

    shas = [candidate.sha for candidate in candidates]
    assert len(candidates) == 2
    assert len(set(shas)) == 2


def test_select_commits_is_deterministic():
    candidates = [
        MODULE.CommitCandidate(sha=f"sha-{index}", committed_at=f"2025-01-0{index}T00:00:00+00:00")
        for index in range(1, 6)
    ]

    first = MODULE.select_commits(
        candidates,
        sample_count=3,
        seed=7,
        pinned_refs=[],
    )
    second = MODULE.select_commits(
        candidates,
        sample_count=3,
        seed=7,
        pinned_refs=[],
    )

    assert [item.sha for item in first] == [item.sha for item in second]


def test_analyze_manifest_writes_quality_outputs_and_summary(
    git_remote: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
):
    manifest_path = tmp_path / "repos.json"
    output_dir = tmp_path / "out"
    _write_json(
        manifest_path,
        {
            "default_sample_count": 2,
            "seed": 123,
            "output_dir": str(output_dir),
            "repos": [
                {
                    "name": "sample-repo",
                    "url": str(git_remote),
                }
            ],
        },
    )

    monkeypatch.setattr(MODULE, "fetch_github_stars", lambda *args, **kwargs: "42")
    monkeypatch.setattr(MODULE, "get_progress_timestamp", lambda: "12:34:56")

    def fake_run_metrics(
        snapshot_dir: Path,
        entry_file: Path,
        artifact_dir: Path,
    ) -> dict[str, float | int]:
        quality_dir = artifact_dir / "quality_analysis"
        quality_dir.mkdir(parents=True, exist_ok=True)
        (quality_dir / "overall_quality.json").write_text("{}")
        (quality_dir / "files.jsonl").write_text("")
        (quality_dir / "symbols.jsonl").write_text("")
        (quality_dir / "ast_grep.jsonl").write_text("")
        return {
            "file_count": 2,
            "loc": 20,
            "violation_pct": 0.1,
            "ast_grep_violations": 3,
            "ast_grep_rules_checked": 7,
            "clone_ratio": 0.05,
            "verbosity": 0.15,
            "erosion": 0.25,
        }

    monkeypatch.setattr(MODULE, "run_metrics_for_snapshot", fake_run_metrics)

    rows = MODULE.analyze_manifest(manifest_path)
    output = capsys.readouterr().out

    ok_rows = [row for row in rows if row.status == "ok"]
    assert len(ok_rows) == 2
    assert all(row.stars == "42" for row in ok_rows)
    assert (output_dir / "summary.json").exists()
    assert (output_dir / "summary.csv").exists()
    assert "12:34:56" in output
    assert "ready on branch" in output
    assert "stars=42" in output
    assert "LOC=20" in output
    assert "files=2" in output
    assert "completed" in output

    for row in ok_rows:
        assert row.snapshot_path is not None
        assert str(output_dir) in row.snapshot_path
        quality_dir = Path(row.snapshot_path) / "quality_analysis"
        assert (quality_dir / "overall_quality.json").exists()
        assert (quality_dir / "files.jsonl").exists()
        assert (quality_dir / "symbols.jsonl").exists()
        assert (quality_dir / "ast_grep.jsonl").exists()


def test_analyze_manifest_records_repo_failure(tmp_path: Path):
    manifest_path = tmp_path / "repos.json"
    _write_json(
        manifest_path,
        {
            "repos": [
                {
                    "name": "broken",
                    "url": "https://example.invalid/not-a-repo.git",
                }
            ]
        },
    )

    rows = MODULE.analyze_manifest(manifest_path, output_dir=tmp_path / "out")

    assert len(rows) == 1
    assert rows[0].status == "repo_failed"


def test_analyze_manifest_skips_missing_url_entry(tmp_path: Path):
    manifest_path = tmp_path / "repos.json"
    _write_json(
        manifest_path,
        {
            "repos": [
                {
                    "name": "python-projects",
                    "stars": "2.1k",
                    "url": None,
                    "note": "multiple repos with this name",
                }
            ]
        },
    )

    rows = MODULE.analyze_manifest(manifest_path, output_dir=tmp_path / "out")

    assert len(rows) == 1
    assert rows[0].status == "skipped"
    assert rows[0].repo_url is None
    assert rows[0].stars == "2.1k"
    assert rows[0].note == "multiple repos with this name"
    assert rows[0].error == "No URL configured."


def test_analyze_manifest_prefers_manifest_stars_over_lookup(
    git_remote: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    manifest_path = tmp_path / "repos.json"
    output_dir = tmp_path / "out"
    _write_json(
        manifest_path,
        {
            "default_sample_count": 1,
            "output_dir": str(output_dir),
            "repos": [
                {
                    "name": "sample-repo",
                    "url": str(git_remote),
                    "stars": "11.3k",
                    "note": "from paper",
                }
            ],
        },
    )

    def fail_fetch(*_args, **_kwargs):
        raise AssertionError("star lookup should not be called")

    monkeypatch.setattr(MODULE, "fetch_github_stars", fail_fetch)

    def fake_run_metrics(
        snapshot_dir: Path,
        entry_file: Path,
        artifact_dir: Path,
    ) -> dict[str, float | int]:
        quality_dir = artifact_dir / "quality_analysis"
        quality_dir.mkdir(parents=True, exist_ok=True)
        (quality_dir / "overall_quality.json").write_text("{}")
        (quality_dir / "files.jsonl").write_text("")
        (quality_dir / "symbols.jsonl").write_text("")
        (quality_dir / "ast_grep.jsonl").write_text("")
        return {
            "file_count": 1,
            "loc": 10,
            "violation_pct": 0.0,
            "ast_grep_violations": 0,
            "ast_grep_rules_checked": 1,
            "clone_ratio": 0.0,
            "verbosity": 0.0,
            "erosion": 0.0,
        }

    monkeypatch.setattr(MODULE, "run_metrics_for_snapshot", fake_run_metrics)

    rows = MODULE.analyze_manifest(manifest_path)

    assert len(rows) == 1
    assert rows[0].status == "ok"
    assert rows[0].stars == "11.3k"
    assert rows[0].note == "from paper"


def test_analyze_manifest_skips_named_repo_filter(
    git_remote: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    manifest_path = tmp_path / "repos.json"
    output_dir = tmp_path / "out"
    _write_json(
        manifest_path,
        {
            "default_sample_count": 1,
            "output_dir": str(output_dir),
            "repos": [
                {
                    "name": "skip-me",
                    "url": str(git_remote),
                },
                {
                    "name": "keep-me",
                    "url": str(git_remote),
                },
            ],
        },
    )

    monkeypatch.setattr(MODULE, "fetch_github_stars", lambda *args, **kwargs: "42")

    def fake_run_metrics(
        snapshot_dir: Path,
        entry_file: Path,
        artifact_dir: Path,
    ) -> dict[str, float | int]:
        quality_dir = artifact_dir / "quality_analysis"
        quality_dir.mkdir(parents=True, exist_ok=True)
        (quality_dir / "overall_quality.json").write_text("{}")
        (quality_dir / "files.jsonl").write_text("")
        (quality_dir / "symbols.jsonl").write_text("")
        (quality_dir / "ast_grep.jsonl").write_text("")
        return {
            "file_count": 1,
            "loc": 10,
            "violation_pct": 0.0,
            "ast_grep_violations": 0,
            "ast_grep_rules_checked": 1,
            "clone_ratio": 0.0,
            "verbosity": 0.0,
            "erosion": 0.0,
        }

    monkeypatch.setattr(MODULE, "run_metrics_for_snapshot", fake_run_metrics)

    rows = MODULE.analyze_manifest(
        manifest_path,
        skip_repos=("skip-me",),
    )

    assert len(rows) == 1
    assert rows[0].status == "ok"
    assert rows[0].repo_key == "keep-me"


def test_discover_entry_file_prefers_explicit_path(tmp_path: Path):
    (tmp_path / "main.py").write_text("print('x')\n")
    (tmp_path / "other.py").write_text("print('y')\n")

    entry = MODULE.discover_entry_file(tmp_path, {".py"}, "other.py")

    assert entry.name == "other.py"


def test_run_metrics_for_snapshot_passes_entry_file_relative_to_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
):
    snapshot_dir = tmp_path / "snapshot"
    artifact_dir = tmp_path / "artifacts"
    entry_file = snapshot_dir / "pkg" / "core.py"
    entry_file.parent.mkdir(parents=True)
    entry_file.write_text("def core():\n    return 1\n")

    captured: dict[str, object] = {}

    class _FakeQualityResult:
        file_count = 1

        class ast_grep:  # noqa: N801
            violations = 0
            rules_checked = 0

    def fake_measure_snapshot_quality(
        entry_arg,
        snapshot_arg,
        *,
        timing_callback=None,
    ):
        captured["entry_arg"] = entry_arg
        captured["snapshot_arg"] = snapshot_arg
        if timing_callback is not None:
            timing_callback(
                {
                    "ast_grep": 2.5,
                    "symbol": 1.25,
                    "aggregate_metrics": 0.125,
                }
            )
        return _FakeQualityResult(), []

    monkeypatch.setattr(MODULE, "measure_snapshot_quality", fake_measure_snapshot_quality)
    def fake_save_quality_metrics(output_arg, *_args, **_kwargs):
        captured["output_arg"] = output_arg

    monkeypatch.setattr(MODULE, "save_quality_metrics", fake_save_quality_metrics)
    monkeypatch.setattr(
        MODULE,
        "get_quality_metrics",
        lambda *args, **kwargs: {
            "loc": 11,
            "violation_pct": 0.25,
            "cloned_pct": 0.5,
        },
    )
    monkeypatch.setattr(MODULE, "compute_checkpoint_verbosity", lambda *args, **kwargs: 0.75)
    monkeypatch.setattr(MODULE, "compute_checkpoint_erosion", lambda *args, **kwargs: 0.9)
    monkeypatch.setattr(MODULE, "get_progress_timestamp", lambda: "12:34:56")

    MODULE.run_metrics_for_snapshot(
        snapshot_dir,
        entry_file,
        artifact_dir,
        debug=True,
        repo_label="[1/1] sample-repo",
        commit_label="commit 1/1 [bold]abc12345[/bold]",
    )
    output = capsys.readouterr().out

    assert captured["snapshot_arg"] == snapshot_dir
    assert captured["entry_arg"] == Path("pkg/core.py")
    assert captured["output_arg"] == artifact_dir
    assert "12:34:56" in output
    assert "measure_snapshot_quality" in output
    assert "quality_analysis" in output
    assert "computing" in output
    assert "metric families" in output
    assert "timings" in output
    assert "ast_grep=2.5s" in output
    assert "symbol=1.2s" in output
    assert "violation_pct" in output
    assert "clone_ratio" in output
    assert "verbosity" in output
    assert "erosion" in output
