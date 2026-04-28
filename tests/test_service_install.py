"""Tests for the per-platform service registration paths."""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from autoclaude import service_install
from autoclaude.service_install import (
    HEARTBEAT_LABEL,
    HEARTBEAT_SCHTASKS_NAME,
    HEARTBEAT_SYSTEMD_UNIT,
    SCHEDULER_LABEL,
    SCHEDULER_SCHTASKS_NAME,
    SCHEDULER_SYSTEMD_UNIT,
    ServiceInstallError,
)


def _ok(stdout: str = "", stderr: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=[], returncode=0, stdout=stdout, stderr=stderr)


def _fail(stderr: str = "boom") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=[], returncode=1, stdout="", stderr=stderr)


def test_install_macos_heartbeat_writes_plist_and_calls_launchctl(tmp_path: Path, monkeypatch) -> None:
    plist_path = tmp_path / "heartbeat.plist"
    monkeypatch.setattr(service_install, "_macos_plist_path", lambda kind: plist_path)
    monkeypatch.setattr(service_install.os, "getuid", lambda: 501)
    monkeypatch.setattr(service_install, "_resolve_autoclaude_binary", lambda: "/usr/local/bin/autoclaude")
    run_mock = MagicMock(return_value=_ok())
    monkeypatch.setattr(service_install, "_run", run_mock)

    result = service_install._macos_bootstrap("heartbeat", "/usr/local/bin/autoclaude", "default")  # noqa: SLF001

    plist_text = plist_path.read_text()
    assert HEARTBEAT_LABEL in plist_text
    assert "/usr/local/bin/autoclaude" in plist_text
    assert "<string>daemon</string>" in plist_text
    assert "--profile" in plist_text and "default" in plist_text
    cmds = [call.args[0] for call in run_mock.call_args_list]
    assert ["launchctl", "bootout", f"gui/501/{HEARTBEAT_LABEL}"] in cmds
    assert ["launchctl", "enable", f"gui/501/{HEARTBEAT_LABEL}"] in cmds
    assert any(c[:3] == ["launchctl", "bootstrap", "gui/501"] for c in cmds)
    assert result.platform == "darwin"
    assert result.action == "installed"


def test_install_macos_scheduler_uses_scheduler_subcommand(tmp_path: Path, monkeypatch) -> None:
    plist_path = tmp_path / "scheduler.plist"
    monkeypatch.setattr(service_install, "_macos_plist_path", lambda kind: plist_path)
    monkeypatch.setattr(service_install.os, "getuid", lambda: 501)
    monkeypatch.setattr(service_install, "_run", MagicMock(return_value=_ok()))

    service_install._macos_bootstrap("scheduler", "/usr/local/bin/autoclaude", "default")  # noqa: SLF001

    plist_text = plist_path.read_text()
    assert SCHEDULER_LABEL in plist_text
    assert "<string>scheduler</string>" in plist_text


def test_install_macos_raises_on_bootstrap_failure(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(service_install, "_macos_plist_path", lambda kind: tmp_path / "agent.plist")
    monkeypatch.setattr(service_install.os, "getuid", lambda: 501)
    run_mock = MagicMock(side_effect=[_ok(), _ok(), _fail("Load failed")])
    monkeypatch.setattr(service_install, "_run", run_mock)
    with pytest.raises(ServiceInstallError, match="Load failed"):
        service_install._macos_bootstrap("heartbeat", "/usr/local/bin/autoclaude", "default")  # noqa: SLF001


def test_install_linux_writes_unit_and_enables(tmp_path: Path, monkeypatch) -> None:
    unit_path = tmp_path / HEARTBEAT_SYSTEMD_UNIT
    monkeypatch.setattr(service_install, "_systemd_unit_path", lambda kind: unit_path)
    run_mock = MagicMock(return_value=_ok())
    monkeypatch.setattr(service_install, "_run", run_mock)

    result = service_install._systemd_install("heartbeat", "/usr/bin/autoclaude", "ci")  # noqa: SLF001

    unit_text = unit_path.read_text()
    assert "ExecStart=/usr/bin/autoclaude daemon --profile ci" in unit_text
    cmds = [call.args[0] for call in run_mock.call_args_list]
    assert ["systemctl", "--user", "daemon-reload"] in cmds
    assert ["systemctl", "--user", "enable", "--now", HEARTBEAT_SYSTEMD_UNIT] in cmds
    assert result.platform == "linux"


def test_install_linux_scheduler_uses_scheduler_subcommand(tmp_path: Path, monkeypatch) -> None:
    unit_path = tmp_path / SCHEDULER_SYSTEMD_UNIT
    monkeypatch.setattr(service_install, "_systemd_unit_path", lambda kind: unit_path)
    monkeypatch.setattr(service_install, "_run", MagicMock(return_value=_ok()))

    service_install._systemd_install("scheduler", "/usr/bin/autoclaude", "default")  # noqa: SLF001

    assert "ExecStart=/usr/bin/autoclaude scheduler --profile default" in unit_path.read_text()


def test_install_windows_creates_scheduled_task(monkeypatch) -> None:
    run_mock = MagicMock(return_value=_ok())
    monkeypatch.setattr(service_install, "_run", run_mock)
    result = service_install._windows_install("heartbeat", r"C:\\autoclaude.exe", "default")  # noqa: SLF001
    create_call = run_mock.call_args_list[0].args[0]
    assert create_call[0].endswith("schtasks.exe")
    assert "/Create" in create_call
    assert HEARTBEAT_SCHTASKS_NAME in create_call
    assert any("autoclaude.exe" in piece for piece in create_call)
    assert any("daemon" in piece for piece in create_call)
    assert result.platform == "win32"


def test_uninstall_macos_removes_plist_and_calls_bootout(tmp_path: Path, monkeypatch) -> None:
    plist = tmp_path / "agent.plist"
    plist.write_text("<plist/>")
    monkeypatch.setattr(service_install, "_macos_plist_path", lambda kind: plist)
    monkeypatch.setattr(service_install.os, "getuid", lambda: 501)
    run_mock = MagicMock(return_value=_ok())
    monkeypatch.setattr(service_install, "_run", run_mock)
    service_install._macos_bootout("heartbeat", remove_plist=True)  # noqa: SLF001
    assert not plist.exists()
    assert run_mock.call_args.args[0][:3] == ["launchctl", "bootout", f"gui/501/{HEARTBEAT_LABEL}"]


def test_install_service_dispatches_by_platform(monkeypatch) -> None:
    sentinel = service_install.InstallResult(platform="darwin", action="installed", detail="x")
    monkeypatch.setattr(service_install.sys, "platform", "darwin")
    monkeypatch.setattr(service_install, "_macos_bootstrap", lambda *_args, **_kwargs: sentinel)
    monkeypatch.setattr(service_install, "_resolve_autoclaude_binary", lambda: "/x")
    assert service_install.install_service("heartbeat", "default") is sentinel


def test_install_service_unsupported_platform_raises(monkeypatch) -> None:
    monkeypatch.setattr(service_install.sys, "platform", "freebsd13")
    monkeypatch.setattr(service_install, "_resolve_autoclaude_binary", lambda: "/x")
    with pytest.raises(ServiceInstallError, match="freebsd13"):
        service_install.install_service("heartbeat", "default")


def test_install_all_calls_both_kinds(monkeypatch) -> None:
    calls: list[tuple[str, str]] = []

    def fake_install(kind: str, profile: str) -> service_install.InstallResult:
        calls.append((kind, profile))
        return service_install.InstallResult(platform="t", action="installed", detail=kind)

    monkeypatch.setattr(service_install, "install_service", fake_install)
    monkeypatch.setattr(service_install, "_remove_legacy", lambda: None)

    results = service_install.install_all("default")

    assert [k for k, _ in calls] == ["heartbeat", "scheduler"]
    assert {r.detail for r in results} == {"heartbeat", "scheduler"}


def test_pause_scheduler_macos_disables_label(monkeypatch) -> None:
    monkeypatch.setattr(service_install.sys, "platform", "darwin")
    monkeypatch.setattr(service_install.os, "getuid", lambda: 501)
    run_mock = MagicMock(return_value=_ok())
    monkeypatch.setattr(service_install, "_run", run_mock)
    monkeypatch.setattr(service_install, "_macos_plist_path", lambda kind: Path("/tmp/x.plist"))

    service_install.pause_scheduler()

    cmds = [c.args[0] for c in run_mock.call_args_list]
    assert ["launchctl", "disable", f"gui/501/{SCHEDULER_LABEL}"] in cmds
    assert ["launchctl", "bootout", f"gui/501/{SCHEDULER_LABEL}"] in cmds


def test_play_scheduler_reinstalls(monkeypatch) -> None:
    sentinel = service_install.InstallResult(platform="t", action="installed", detail="scheduler")
    monkeypatch.setattr(service_install, "install_service", lambda kind, profile: sentinel if kind == "scheduler" else None)
    assert service_install.play_scheduler("default") is sentinel


def test_resolve_autoclaude_binary_prefers_env_override(monkeypatch) -> None:
    monkeypatch.setenv("AUTOCLAUDE_BINARY", "/opt/bin/autoclaude")
    assert service_install._resolve_autoclaude_binary() == "/opt/bin/autoclaude"  # noqa: SLF001


def test_resolve_autoclaude_binary_falls_back_to_module(monkeypatch) -> None:
    monkeypatch.delenv("AUTOCLAUDE_BINARY", raising=False)
    with patch.object(service_install.shutil, "which", return_value=None):
        result = service_install._resolve_autoclaude_binary()  # noqa: SLF001
    assert result.endswith("-m autoclaude.cli")
