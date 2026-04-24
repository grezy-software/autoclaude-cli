"""Preflight checks for the GitHub CLI dependency."""

from __future__ import annotations

from typing import Any

import pytest

from autoclaude import gh, workspace


def test_ensure_installed_passes_when_gh_on_path(monkeypatch) -> None:
    monkeypatch.setattr(gh.shutil, "which", lambda name: "/usr/local/bin/gh" if name == "gh" else None)
    gh.ensure_installed()  # must not raise


def test_ensure_installed_raises_when_missing(monkeypatch) -> None:
    monkeypatch.setattr(gh.shutil, "which", lambda _name: None)
    with pytest.raises(gh.GhError):
        gh.ensure_installed()


def test_is_authenticated_false_when_gh_missing(monkeypatch) -> None:
    monkeypatch.setattr(gh.shutil, "which", lambda _name: None)
    assert gh.is_authenticated() is False


def test_is_authenticated_reflects_subprocess_exit(monkeypatch) -> None:
    monkeypatch.setattr(gh.shutil, "which", lambda _name: "/usr/local/bin/gh")

    class _Result:
        def __init__(self, code: int) -> None:
            self.returncode = code

    monkeypatch.setattr(gh.subprocess, "run", lambda *args, **kwargs: _Result(0))  # noqa: ARG005
    assert gh.is_authenticated() is True

    monkeypatch.setattr(gh.subprocess, "run", lambda *args, **kwargs: _Result(1))  # noqa: ARG005
    assert gh.is_authenticated() is False


def test_workspace_sync_aborts_when_gh_missing(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(workspace, "ensure_gh_installed", _raise_gh_missing)

    (tmp_path / "src").mkdir()
    (tmp_path / "src" / ".git").mkdir()  # just needs to look like a repo; sync checks gh first

    ws = workspace.Workspace.for_source(tmp_path / "src", home=tmp_path / "home")
    with pytest.raises(workspace.WorkspaceError) as err:
        ws.sync(tmp_path / "src")
    assert "github" in str(err.value).lower()


def _raise_gh_missing(*_args: Any, **_kwargs: Any) -> None:
    raise gh.GhError("GitHub CLI not found.")
