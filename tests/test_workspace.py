"""Tests for the autoclaude workspace (clones + per-tick worktrees)."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from autoclaude.workspace import (
    AUTOCLAUDE_HOME_ENV,
    Workspace,
    WorkspaceError,
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


def test_derive_slug_disambiguates_same_basename(tmp_path) -> None:
    a = tmp_path / "one" / "nango"
    b = tmp_path / "two" / "nango"
    a.mkdir(parents=True)
    b.mkdir(parents=True)
    slug_a = derive_slug(a)
    slug_b = derive_slug(b)
    assert slug_a != slug_b
    assert slug_a.startswith("nango-")
    assert slug_b.startswith("nango-")


def test_sync_clones_first_then_fetches(tmp_path) -> None:
    source = _make_source_repo(tmp_path / "src")
    home = tmp_path / "home"
    workspace = Workspace.for_source(source, home=home)

    workspace.sync(source)
    assert (workspace.clone_path / ".git").exists()

    # Second commit in source should be pulled in by a subsequent sync.
    (source / "NEW.md").write_text("new\n", encoding="utf-8")
    _git(["add", "NEW.md"], cwd=source)
    _git(["commit", "-q", "-m", "second"], cwd=source)
    workspace.sync(source)

    log = subprocess.run(
        ["git", "log", "--format=%s", "origin/main"],
        cwd=str(workspace.clone_path),
        capture_output=True,
        text=True,
        check=True,
    )
    assert "second" in log.stdout


def test_sync_rejects_non_repo(tmp_path) -> None:
    not_a_repo = tmp_path / "plain"
    not_a_repo.mkdir()
    workspace = Workspace.for_source(not_a_repo, home=tmp_path / "home")
    with pytest.raises(WorkspaceError):
        workspace.sync(not_a_repo)


def test_create_worktree_creates_branch(tmp_path) -> None:
    source = _make_source_repo(tmp_path / "src")
    workspace = Workspace.for_source(source, home=tmp_path / "home")
    workspace.sync(source)

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
    workspace = Workspace.for_source(source, home=tmp_path / "home")
    workspace.sync(source)
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


def test_create_worktree_replaces_stale_directory(tmp_path) -> None:
    source = _make_source_repo(tmp_path / "src")
    workspace = Workspace.for_source(source, home=tmp_path / "home")
    workspace.sync(source)

    # Simulate a stale worktree dir left from a previous run.
    stale = workspace.worktree_path(99)
    stale.parent.mkdir(parents=True, exist_ok=True)
    stale.mkdir()
    (stale / "leftover.txt").write_text("stale\n", encoding="utf-8")

    worktree = workspace.create_worktree(99)
    assert worktree.path == stale
    assert not (stale / "leftover.txt").exists()
