"""Tests for the ``status`` command's autoclaude-state line."""

from __future__ import annotations

import pytest

from autoclaude import cli
from autoclaude.config import Profile
from autoclaude.service_install import InstallResult, ServiceInstallError


def _install_result(detail: str) -> InstallResult:
    return InstallResult(platform="linux", action="status", detail=detail)


def _patch_services(
    monkeypatch: pytest.MonkeyPatch,
    *,
    scheduler: str = "active",
    heartbeat: str = "active",
) -> None:
    def _fake(kind: str) -> InstallResult:
        if kind == "scheduler":
            return _install_result(scheduler)
        if kind == "heartbeat":
            return _install_result(heartbeat)
        msg = f"unexpected kind {kind!r}"
        raise AssertionError(msg)

    monkeypatch.setattr(cli, "status_service", _fake)


def test_resolve_status_running_when_all_active(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_services(monkeypatch)
    prof = Profile(name="default", paused=False)
    level, label = cli._resolve_autoclaude_status(prof)  # noqa: SLF001
    assert level == "running"
    assert "running" in label.lower()


def test_resolve_status_paused_when_profile_flag_set(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_services(monkeypatch)
    prof = Profile(name="default", paused=True)
    level, label = cli._resolve_autoclaude_status(prof)  # noqa: SLF001
    assert level == "paused"
    assert "profile paused" in label


def test_resolve_status_paused_when_scheduler_stopped(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_services(monkeypatch, scheduler="inactive")
    prof = Profile(name="default", paused=False)
    level, label = cli._resolve_autoclaude_status(prof)  # noqa: SLF001
    assert level == "paused"
    assert "scheduler inactive" in label


def test_resolve_status_paused_combines_profile_and_scheduler(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_services(monkeypatch, scheduler="inactive")
    prof = Profile(name="default", paused=True)
    level, label = cli._resolve_autoclaude_status(prof)  # noqa: SLF001
    assert level == "paused"
    assert "profile paused" in label
    assert "scheduler inactive" in label


def test_resolve_status_degraded_when_only_heartbeat_down(monkeypatch: pytest.MonkeyPatch) -> None:
    """Scheduler still ticks; only the dashboard liveness signal is dark."""
    _patch_services(monkeypatch, heartbeat="inactive")
    prof = Profile(name="default", paused=False)
    level, label = cli._resolve_autoclaude_status(prof)  # noqa: SLF001
    assert level == "degraded"
    assert "heartbeat inactive" in label


def test_resolve_status_paused_takes_priority_over_degraded(monkeypatch: pytest.MonkeyPatch) -> None:
    """When ticks won't run AND the heartbeat is also dark, the user-visible state is 'paused'."""
    _patch_services(monkeypatch, scheduler="inactive", heartbeat="inactive")
    prof = Profile(name="default", paused=False)
    level, label = cli._resolve_autoclaude_status(prof)  # noqa: SLF001
    assert level == "paused"
    assert "scheduler inactive" in label
    assert "heartbeat inactive" in label


def test_resolve_status_handles_service_install_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """A service-install error must not break the status line."""

    def _boom(_kind: str) -> InstallResult:
        raise ServiceInstallError("systemctl missing")

    monkeypatch.setattr(cli, "status_service", _boom)
    prof = Profile(name="default", paused=False)
    level, label = cli._resolve_autoclaude_status(prof)  # noqa: SLF001
    assert level == "paused"
    assert "error: systemctl missing" in label
