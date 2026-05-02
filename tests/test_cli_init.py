"""Tests for the ``init`` command's autoclaude-user provisioning flow."""

from __future__ import annotations

from pathlib import Path

import pytest

from autoclaude import claude_env, cli, creds_watcher


@pytest.fixture(autouse=True)
def _reset_module_caches() -> None:
    claude_env.reset_caches()


@pytest.fixture()
def _no_share_helpers(monkeypatch: pytest.MonkeyPatch) -> None:
    """Default: stub the install-time chgrp helpers so they don't touch the host."""
    monkeypatch.setattr(claude_env, "share_claude_config", lambda *_a, **_kw: None)
    monkeypatch.setattr(claude_env, "share_claude_binary", lambda *_a, **_kw: None)
    monkeypatch.setattr(claude_env, "share_gh_config", lambda *_a, **_kw: None)
    monkeypatch.setattr(claude_env, "share_workspace_home", lambda *_a, **_kw: None)
    monkeypatch.setattr(
        creds_watcher,
        "install_watcher",
        lambda *_a, **_kw: creds_watcher.WatcherInstallResult(action="skipped", detail="stubbed"),
    )


def test_provision_skipped_when_not_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    _no_share_helpers: None,
) -> None:
    """Non-root host: never prompts, never creates the user."""
    monkeypatch.setattr(claude_env.os, "geteuid", lambda: 1000)

    def _fail_if_called(*_a: object, **_kw: object) -> bool:
        msg = "should not prompt outside of root"
        raise AssertionError(msg)

    monkeypatch.setattr(cli.typer, "confirm", _fail_if_called)
    cli._provision_autoclaude_runtime(cwd=tmp_path, force=False, interactive=True)  # noqa: SLF001


def test_provision_skipped_when_auto_mode(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    _no_share_helpers: None,
) -> None:
    """Root + defaultMode=auto: claude is fine without bypassPermissions, no user needed."""
    monkeypatch.setattr(claude_env.os, "geteuid", lambda: 0)
    monkeypatch.setattr(claude_env, "should_bypass_permissions", lambda **_kw: False)

    def _fail_if_called(*_a: object, **_kw: object) -> bool:
        msg = "should not prompt in auto mode"
        raise AssertionError(msg)

    monkeypatch.setattr(cli.typer, "confirm", _fail_if_called)
    cli._provision_autoclaude_runtime(cwd=tmp_path, force=False, interactive=True)  # noqa: SLF001


def test_provision_skips_creation_when_user_exists_but_still_grants_perms(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """User already provisioned: skip useradd, but still re-apply share_* (idempotent)."""
    monkeypatch.setattr(claude_env.os, "geteuid", lambda: 0)
    monkeypatch.setattr(claude_env, "should_bypass_permissions", lambda **_kw: True)
    monkeypatch.setattr(claude_env, "autoclaude_user_exists", lambda: True)

    def _fail_if_called(*_a: object, **_kw: object) -> bool:
        msg = "should not prompt when user already exists"
        raise AssertionError(msg)

    monkeypatch.setattr(cli.typer, "confirm", _fail_if_called)
    created: list[bool] = []
    monkeypatch.setattr(claude_env, "ensure_autoclaude_user", lambda *_a, **_kw: created.append(True))
    share_calls: list[str] = []
    monkeypatch.setattr(claude_env, "share_claude_config", lambda *_a, **_kw: share_calls.append("config"))
    monkeypatch.setattr(claude_env, "share_claude_binary", lambda *_a, **_kw: share_calls.append("binary"))
    monkeypatch.setattr(claude_env, "share_gh_config", lambda *_a, **_kw: share_calls.append("gh"))
    monkeypatch.setattr(claude_env, "share_workspace_home", lambda *_a, **_kw: share_calls.append("workspace"))
    watcher_calls: list[str] = []

    def _record_watcher_install(*_a: object, **_kw: object) -> creds_watcher.WatcherInstallResult:
        watcher_calls.append("install")
        return creds_watcher.WatcherInstallResult(action="installed", detail="stub.service")

    monkeypatch.setattr(creds_watcher, "install_watcher", _record_watcher_install)

    cli._provision_autoclaude_runtime(cwd=tmp_path, force=False, interactive=True)  # noqa: SLF001
    assert created == []
    assert share_calls == ["config", "binary", "gh", "workspace"]
    assert watcher_calls == ["install"]


def test_provision_prompts_and_creates_when_confirmed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    _no_share_helpers: None,
) -> None:
    monkeypatch.setattr(claude_env.os, "geteuid", lambda: 0)
    monkeypatch.setattr(claude_env, "should_bypass_permissions", lambda **_kw: True)
    monkeypatch.setattr(claude_env, "autoclaude_user_exists", lambda: False)

    prompts: list[str] = []

    def _capture_confirm(message: str, default: bool = True) -> bool:  # noqa: FBT001, FBT002, ARG001
        prompts.append(message)
        return True

    monkeypatch.setattr(cli.typer, "confirm", _capture_confirm)
    created: list[bool] = []
    monkeypatch.setattr(claude_env, "ensure_autoclaude_user", lambda *_a, **_kw: created.append(True))

    cli._provision_autoclaude_runtime(cwd=tmp_path, force=False, interactive=True)  # noqa: SLF001
    assert len(prompts) == 1
    assert "autoclaude" in prompts[0]
    assert created == [True]


def test_provision_skips_creation_when_declined(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    _no_share_helpers: None,
) -> None:
    monkeypatch.setattr(claude_env.os, "geteuid", lambda: 0)
    monkeypatch.setattr(claude_env, "should_bypass_permissions", lambda **_kw: True)
    monkeypatch.setattr(claude_env, "autoclaude_user_exists", lambda: False)
    monkeypatch.setattr(cli.typer, "confirm", lambda *_a, **_kw: False)

    created: list[bool] = []
    monkeypatch.setattr(claude_env, "ensure_autoclaude_user", lambda *_a, **_kw: created.append(True))
    cli._provision_autoclaude_runtime(cwd=tmp_path, force=False, interactive=True)  # noqa: SLF001
    assert created == []


def test_provision_handles_user_creation_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    _no_share_helpers: None,
) -> None:
    """A UserCreationError must be caught and surfaced, never propagated out of init."""
    monkeypatch.setattr(claude_env.os, "geteuid", lambda: 0)
    monkeypatch.setattr(claude_env, "should_bypass_permissions", lambda **_kw: True)
    monkeypatch.setattr(claude_env, "autoclaude_user_exists", lambda: False)
    monkeypatch.setattr(cli.typer, "confirm", lambda *_a, **_kw: True)

    def _boom(*_a: object, **_kw: object) -> None:
        raise claude_env.UserCreationError("useradd not available")

    monkeypatch.setattr(claude_env, "ensure_autoclaude_user", _boom)
    # Must not raise.
    cli._provision_autoclaude_runtime(cwd=tmp_path, force=False, interactive=True)  # noqa: SLF001


# --- --user-autoclaude flag (force=True, interactive=False) -----------------


def test_force_flag_creates_without_prompting(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    _no_share_helpers: None,
) -> None:
    """`init --user-autoclaude` skips the prompt and creates the user directly."""
    monkeypatch.setattr(claude_env.os, "geteuid", lambda: 0)
    monkeypatch.setattr(claude_env, "should_bypass_permissions", lambda **_kw: True)
    monkeypatch.setattr(claude_env, "autoclaude_user_exists", lambda: False)

    def _fail_if_called(*_a: object, **_kw: object) -> bool:
        msg = "must not prompt when --user-autoclaude was passed"
        raise AssertionError(msg)

    monkeypatch.setattr(cli.typer, "confirm", _fail_if_called)
    created: list[bool] = []
    monkeypatch.setattr(claude_env, "ensure_autoclaude_user", lambda *_a, **_kw: created.append(True))

    cli._provision_autoclaude_runtime(cwd=tmp_path, force=True, interactive=False)  # noqa: SLF001
    assert created == [True]


def test_force_flag_runs_even_in_auto_mode(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    _no_share_helpers: None,
) -> None:
    """When the user explicitly passes --user-autoclaude, provision regardless of defaultMode."""
    monkeypatch.setattr(claude_env.os, "geteuid", lambda: 0)
    monkeypatch.setattr(claude_env, "should_bypass_permissions", lambda **_kw: False)  # would skip without force
    monkeypatch.setattr(claude_env, "autoclaude_user_exists", lambda: False)
    monkeypatch.setattr(cli.typer, "confirm", lambda *_a, **_kw: False)  # not called either way
    created: list[bool] = []
    monkeypatch.setattr(claude_env, "ensure_autoclaude_user", lambda *_a, **_kw: created.append(True))

    cli._provision_autoclaude_runtime(cwd=tmp_path, force=True, interactive=False)  # noqa: SLF001
    assert created == [True]


def test_force_flag_refuses_without_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    _no_share_helpers: None,
) -> None:
    """`init --user-autoclaude` from a non-root user surfaces a clear error and does not create anything."""
    monkeypatch.setattr(claude_env.os, "geteuid", lambda: 1000)
    created: list[bool] = []
    monkeypatch.setattr(claude_env, "ensure_autoclaude_user", lambda *_a, **_kw: created.append(True))

    cli._provision_autoclaude_runtime(cwd=tmp_path, force=True, interactive=False)  # noqa: SLF001
    assert created == []
