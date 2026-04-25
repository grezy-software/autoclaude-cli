"""Tests for the autoclaude workspace (clones + per-tick worktrees)."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

import autoclaude.workspace as wsmod
from autoclaude.workspace import (
    AUTOCLAUDE_HOME_ENV,
    Workspace,
    WorkspaceError,
    _canonical_github_clone_url,
    derive_slug,
    workspace_home,
)


def _git(args: list[str], cwd: Path) -> None:
    subprocess.run(["git", *args], cwd=str(cwd), check=True, capture_output=True, text=True)


def _make_source_repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    _git(["init", "-q", "-b", "main"], cwd=path)
    _git(["config", "user.email", "test@example.com"], cwd=path)
    _git(["config", "user.name", "Test"], cwd=path)
    (path / "README.md").write_text("hello\n", encoding="utf-8")
    _git(["add", "README.md"], cwd=path)
    _git(["commit", "-q", "-m", "initial"], cwd=path)
    return path


def test_workspace_home_honours_env(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv(AUTOCLAUDE_HOME_ENV, str(tmp_path / "custom"))
    assert workspace_home() == tmp_path / "custom"


def test_workspace_home_defaults_to_dot_autoclaude(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv(AUTOCLAUDE_HOME_ENV, raising=False)
    monkeypatch.setattr(Path, "home", classmethod(lambda _cls: tmp_path))
    assert workspace_home() == tmp_path / ".autoclaude"


def test_derive_slug_uses_github_repo() -> None:
    slug = derive_slug("soaria-app/soaria")
    assert slug.startswith("soaria-app-soaria-")
    # Hash suffix is 8 hex chars.
    assert len(slug.split("-")[-1]) == 8


def test_derive_slug_changes_when_repo_renamed() -> None:
    """Slug must include the canonical URL hash so a renamed repo gets a fresh clone dir."""
    a = derive_slug("soaria-app/soaria")
    b = derive_slug("soaria-app/soaria-renamed")
    assert a != b


def test_derive_slug_normalises_input_shapes() -> None:
    plain = derive_slug("soaria-app/soaria")
    full_url = derive_slug("https://github.com/soaria-app/soaria.git")
    assert plain == full_url


def test_for_github_repo_rejects_invalid_input(tmp_path) -> None:
    with pytest.raises(WorkspaceError):
        Workspace.for_github_repo("not a repo", home=tmp_path / "home")


def test_for_github_repo_sets_canonical_clone_url(tmp_path) -> None:
    ws = Workspace.for_github_repo("soaria-app/soaria", home=tmp_path / "home")
    assert ws.clone_url == "https://github.com/soaria-app/soaria.git"


def test_github_workspace_clone_routes_through_gh(tmp_path, monkeypatch) -> None:
    """First-run clone must call `gh repo clone` so the user is never asked for a password."""
    calls: list[tuple[str, list[str]]] = []

    class _GhResult:
        returncode = 0
        stdout = ""
        stderr = ""

    def _fake_gh(args, *, cwd=None, check=True):  # noqa: ARG001
        calls.append(("gh", args))
        # Pretend gh did the clone by creating an empty .git so subsequent
        # logic sees a "real" clone.
        target = Path(args[-1])
        (target / ".git").mkdir(parents=True, exist_ok=True)
        return _GhResult()

    def _fake_git(args, *, cwd=None, check=True):  # noqa: ARG001
        calls.append(("git", args))
        return subprocess.CompletedProcess(args=args, returncode=0, stdout="", stderr="")

    monkeypatch.setattr(wsmod, "_run_gh", _fake_gh)
    monkeypatch.setattr(wsmod, "_git", _fake_git)
    monkeypatch.setattr(wsmod, "ensure_gh_installed", lambda: None)

    ws = Workspace.for_github_repo("soaria-app/soaria", home=tmp_path / "home")
    ws.sync()

    cmds = [(tool, args[0]) for tool, args in calls]
    assert ("gh", "repo") in cmds, f"expected `gh repo clone`, got {cmds!r}"
    # No naked `git clone` — that would prompt for credentials.
    assert ("git", "clone") not in cmds


def test_github_workspace_fetch_uses_gh_credential_helper(tmp_path, monkeypatch) -> None:
    """Subsequent fetches must inject `gh auth git-credential` for one call only."""
    git_calls: list[list[str]] = []

    def _fake_git(args, *, cwd=None, check=True):  # noqa: ARG001
        git_calls.append(args)
        return subprocess.CompletedProcess(args=args, returncode=0, stdout="", stderr="")

    monkeypatch.setattr(wsmod, "_git", _fake_git)
    monkeypatch.setattr(wsmod, "_run_gh", lambda *_a, **_k: None)
    monkeypatch.setattr(wsmod, "ensure_gh_installed", lambda: None)

    ws = Workspace.for_github_repo("soaria-app/soaria", home=tmp_path / "home")
    # Pre-create the clone path so sync skips the initial clone branch.
    ws.clone_path.mkdir(parents=True, exist_ok=True)
    ws.sync()

    fetch_calls = [args for args in git_calls if "fetch" in args]
    assert fetch_calls, "expected at least one git fetch"
    fetch_args = fetch_calls[0]
    assert "credential.helper=!gh auth git-credential" in fetch_args
    # The empty override before our helper guards against an inherited prompt-helper firing first.
    assert fetch_args.count("credential.helper=") >= 1


def test_local_path_workspace_does_not_use_gh_clone(tmp_path) -> None:
    """Local-path test fixtures still use plain `git clone` -- no gh needed for offline tests."""
    source = _make_source_repo(tmp_path / "src")
    ws = Workspace.for_local_path(source, home=tmp_path / "home")
    ws.sync()
    # Sanity: the clone exists and points at the local source.
    url = subprocess.run(
        ["git", "remote", "get-url", "origin"],
        cwd=str(ws.clone_path),
        capture_output=True,
        text=True,
        check=True,
    )
    assert url.stdout.strip() == ws.clone_url


def test_sync_clones_first_then_fetches(tmp_path) -> None:
    source = _make_source_repo(tmp_path / "src")
    home = tmp_path / "home"
    workspace = Workspace.for_local_path(source, home=home)

    workspace.sync()
    assert (workspace.clone_path / ".git").exists()

    # Second commit in source should be pulled in by a subsequent sync.
    (source / "NEW.md").write_text("new\n", encoding="utf-8")
    _git(["add", "NEW.md"], cwd=source)
    _git(["commit", "-q", "-m", "second"], cwd=source)
    workspace.sync()

    log = subprocess.run(
        ["git", "log", "--format=%s", "origin/main"],
        cwd=str(workspace.clone_path),
        capture_output=True,
        text=True,
        check=True,
    )
    assert "second" in log.stdout


def test_sync_origin_points_at_clone_url(tmp_path) -> None:
    """Origin must equal `workspace.clone_url` so `gh` resolves the right repo."""
    source = _make_source_repo(tmp_path / "src")
    workspace = Workspace.for_local_path(source, home=tmp_path / "home")
    workspace.sync()

    url = subprocess.run(
        ["git", "remote", "get-url", "origin"],
        cwd=str(workspace.clone_path),
        capture_output=True,
        text=True,
        check=True,
    )
    assert url.stdout.strip() == workspace.clone_url


def test_create_worktree_creates_branch(tmp_path) -> None:
    source = _make_source_repo(tmp_path / "src")
    workspace = Workspace.for_local_path(source, home=tmp_path / "home")
    workspace.sync()

    worktree = workspace.create_worktree(42)
    assert worktree.path.exists()
    assert worktree.branch == f"autoclaude/{workspace.slug}/tick-42"

    branches = subprocess.run(
        ["git", "branch", "--list", worktree.branch],
        cwd=str(workspace.clone_path),
        capture_output=True,
        text=True,
        check=True,
    )
    assert worktree.branch in branches.stdout


def test_remove_worktree_keeps_branch(tmp_path) -> None:
    source = _make_source_repo(tmp_path / "src")
    workspace = Workspace.for_local_path(source, home=tmp_path / "home")
    workspace.sync()
    worktree = workspace.create_worktree(7)
    workspace.remove_worktree(worktree)

    assert not worktree.path.exists()
    # Branch must still exist so the tick's changes remain discoverable.
    branches = subprocess.run(
        ["git", "branch", "--list", worktree.branch],
        cwd=str(workspace.clone_path),
        capture_output=True,
        text=True,
        check=True,
    )
    assert worktree.branch in branches.stdout


_CANONICAL_URL = "https://github.com/soaria-app/soaria.git"


@pytest.mark.parametrize(
    "value",
    [
        "soaria-app/soaria",
        "soaria-app/soaria.git",
        "https://github.com/soaria-app/soaria",
        "https://github.com/soaria-app/soaria.git",
        "http://github.com/soaria-app/soaria",
        "https://www.github.com/soaria-app/soaria",
        "git@github.com:soaria-app/soaria.git",
        "ssh://git@github.com/soaria-app/soaria.git",
        # The exact double-prefix bug from the production incident.
        "https://github.com/https://github.com/soaria-app/soaria.git",
        # Whitespace and trailing slash.
        "  soaria-app/soaria/  ",
    ],
)
def test_canonical_github_clone_url_normalises(value: str) -> None:
    assert _canonical_github_clone_url(value) == _CANONICAL_URL


@pytest.mark.parametrize(
    "value",
    [
        "",
        "   ",
        "soaria-app",  # missing repo
        "https://github.com/",  # missing owner/repo
        "https://gitlab.com/owner/repo",  # wrong host stays as `owner` segment only
        "not even close",
    ],
)
def test_canonical_github_clone_url_rejects_malformed(value: str) -> None:
    with pytest.raises(ValueError):
        _canonical_github_clone_url(value)


def test_create_worktree_replaces_stale_directory(tmp_path) -> None:
    source = _make_source_repo(tmp_path / "src")
    workspace = Workspace.for_local_path(source, home=tmp_path / "home")
    workspace.sync()

    # Simulate a stale worktree dir left from a previous run.
    stale = workspace.worktree_path(99)
    stale.parent.mkdir(parents=True, exist_ok=True)
    stale.mkdir()
    (stale / "leftover.txt").write_text("stale\n", encoding="utf-8")

    worktree = workspace.create_worktree(99)
    assert worktree.path == stale
    assert not (stale / "leftover.txt").exists()
