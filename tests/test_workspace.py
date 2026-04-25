"""Tests for the autoclaude workspace (clones + per-tick worktrees)."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

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
