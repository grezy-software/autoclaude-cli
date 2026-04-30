"""Tests for the ``status`` command's autoclaude-state line."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from autoclaude import cli
from autoclaude.config import Profile
from autoclaude.scheduler import DEFAULT_INTERVAL_SECONDS as SCHEDULER_DEFAULT_INTERVAL
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


def _write_last_tick(root: Path, ended_at: datetime) -> None:
    state_dir = root / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    payload = {"ended_at": ended_at.isoformat().replace("+00:00", "Z")}
    (state_dir / "last_tick.json").write_text(json.dumps(payload), encoding="utf-8")


def test_resolve_next_tick_returns_eta_when_running(tmp_path: Path) -> None:
    ended_at = datetime.now(tz=UTC) - timedelta(seconds=SCHEDULER_DEFAULT_INTERVAL - 60)
    _write_last_tick(tmp_path, ended_at)
    prof = Profile(name="default", autoclaude_root=str(tmp_path), paused=False)
    color, label = cli._resolve_next_tick(prof, "running")  # noqa: SLF001
    assert color == "green"
    assert label.startswith("in ")
    assert "local)" in label


def test_resolve_next_tick_due_now_when_overdue(tmp_path: Path) -> None:
    ended_at = datetime.now(tz=UTC) - timedelta(seconds=SCHEDULER_DEFAULT_INTERVAL * 2)
    _write_last_tick(tmp_path, ended_at)
    prof = Profile(name="default", autoclaude_root=str(tmp_path), paused=False)
    color, label = cli._resolve_next_tick(prof, "running")  # noqa: SLF001
    assert color == "yellow"
    assert label.startswith("due now")


def test_resolve_next_tick_pending_when_no_history(tmp_path: Path) -> None:
    prof = Profile(name="default", autoclaude_root=str(tmp_path), paused=False)
    color, label = cli._resolve_next_tick(prof, "running")  # noqa: SLF001
    assert color == "dim"
    assert "pending" in label


def test_resolve_next_tick_handles_naive_ended_at(tmp_path: Path) -> None:
    """A naive ``ended_at`` (no Z, no offset) must be assumed UTC, not crash on subtraction."""
    state_dir = tmp_path / "state"
    state_dir.mkdir(parents=True)
    naive_iso = (datetime.now(tz=UTC) - timedelta(seconds=60)).replace(tzinfo=None).isoformat()
    (state_dir / "last_tick.json").write_text(json.dumps({"ended_at": naive_iso}), encoding="utf-8")
    prof = Profile(name="default", autoclaude_root=str(tmp_path), paused=False)
    # Should not raise TypeError on aware/naive subtraction.
    color, label = cli._resolve_next_tick(prof, "running")  # noqa: SLF001
    assert color in {"green", "yellow"}
    assert "unknown" not in label


def test_resolve_next_tick_paused_profile(tmp_path: Path) -> None:
    ended_at = datetime.now(tz=UTC)
    _write_last_tick(tmp_path, ended_at)
    prof = Profile(name="default", autoclaude_root=str(tmp_path), paused=True)
    color, label = cli._resolve_next_tick(prof, "paused")  # noqa: SLF001
    assert color == "yellow"
    assert "profile paused" in label


def test_resolve_next_tick_scheduler_stopped(tmp_path: Path) -> None:
    ended_at = datetime.now(tz=UTC)
    _write_last_tick(tmp_path, ended_at)
    prof = Profile(name="default", autoclaude_root=str(tmp_path), paused=False)
    color, label = cli._resolve_next_tick(prof, "paused")  # noqa: SLF001
    assert color == "yellow"
    assert "scheduler stopped" in label


def test_resolve_next_tick_degraded_still_estimates(tmp_path: Path) -> None:
    """Degraded means heartbeat is dark, but the scheduler still ticks."""
    ended_at = datetime.now(tz=UTC) - timedelta(seconds=60)
    _write_last_tick(tmp_path, ended_at)
    prof = Profile(name="default", autoclaude_root=str(tmp_path), paused=False)
    color, label = cli._resolve_next_tick(prof, "degraded")  # noqa: SLF001
    assert color in {"green", "yellow"}
    assert "scheduler stopped" not in label
    assert "profile paused" not in label


def test_format_relative_seconds_compact() -> None:
    assert cli._format_relative_seconds(0) == "now"  # noqa: SLF001
    assert cli._format_relative_seconds(45) == "45s"  # noqa: SLF001
    assert cli._format_relative_seconds(125) == "2m05s"  # noqa: SLF001
    assert cli._format_relative_seconds(3700) == "1h01m"  # noqa: SLF001
