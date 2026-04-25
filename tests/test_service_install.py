"""Tests for the per-platform service registration paths."""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from autoclaude import service_install
from autoclaude.service_install import (
    LAUNCHD_LABEL,
    SCHTASKS_NAME,
    SYSTEMD_UNIT_NAME,
    ServiceInstallError,
)


def _ok(stdout: str = "", stderr: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=[], returncode=0, stdout=stdout, stderr=stderr)


def _fail(stderr: str = "boom") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=[], returncode=1, stdout="", stderr=stderr)


def test_install_macos_writes_plist_and_calls_launchctl(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(service_install, "_macos_plist_path", lambda: tmp_path / "agent.plist")
    monkeypatch.setattr(service_install.os, "getuid", lambda: 501)
    monkeypatch.setattr(service_install, "_resolve_autoclaude_binary", lambda: "/usr/local/bin/autoclaude")
    run_mock = MagicMock(side_effect=[_ok(), _ok()])
    monkeypatch.setattr(service_install, "_run", run_mock)

    result = service_install.install_macos("/usr/local/bin/autoclaude", "default")

    plist_text = (tmp_path / "agent.plist").read_text()
    assert LAUNCHD_LABEL in plist_text
    assert "/usr/local/bin/autoclaude" in plist_text
    assert "<string>daemon</string>" in plist_text
    assert "--profile" in plist_text and "default" in plist_text
    assert run_mock.call_args_list[0].args[0][:3] == ["launchctl", "bootout", "gui/501/com.grezy.autoclaude"]
    assert run_mock.call_args_list[1].args[0][:3] == ["launchctl", "bootstrap", "gui/501"]
    assert result.platform == "darwin"
    assert result.action == "installed"


def test_install_macos_raises_on_bootstrap_failure(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(service_install, "_macos_plist_path", lambda: tmp_path / "agent.plist")
    monkeypatch.setattr(service_install.os, "getuid", lambda: 501)
    run_mock = MagicMock(side_effect=[_ok(), _fail("Load failed")])
    monkeypatch.setattr(service_install, "_run", run_mock)
    with pytest.raises(ServiceInstallError, match="Load failed"):
        service_install.install_macos("/usr/local/bin/autoclaude", "default")


def test_install_linux_writes_unit_and_enables(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(service_install, "_systemd_unit_path", lambda: tmp_path / SYSTEMD_UNIT_NAME)
    run_mock = MagicMock(side_effect=[_ok(), _ok()])
    monkeypatch.setattr(service_install, "_run", run_mock)

    result = service_install.install_linux("/usr/bin/autoclaude", "ci")

    unit_text = (tmp_path / SYSTEMD_UNIT_NAME).read_text()
    assert "ExecStart=/usr/bin/autoclaude daemon --profile ci" in unit_text
    assert run_mock.call_args_list[0].args[0] == ["systemctl", "--user", "daemon-reload"]
    assert run_mock.call_args_list[1].args[0][-3:] == ["enable", "--now", SYSTEMD_UNIT_NAME]
    assert result.platform == "linux"


def test_install_windows_creates_scheduled_task(monkeypatch) -> None:
    run_mock = MagicMock(side_effect=[_ok(), _ok()])
    monkeypatch.setattr(service_install, "_run", run_mock)
    result = service_install.install_windows(r"C:\\Program Files\\autoclaude\\autoclaude.exe", "default")
    create_call = run_mock.call_args_list[0].args[0]
    assert create_call[0].endswith("schtasks.exe")
    assert "/Create" in create_call
    assert SCHTASKS_NAME in create_call
    assert any("autoclaude.exe" in piece for piece in create_call)
    assert result.platform == "win32"


def test_uninstall_macos_removes_plist_and_calls_bootout(tmp_path: Path, monkeypatch) -> None:
    plist = tmp_path / "agent.plist"
    plist.write_text("<plist/>")
    monkeypatch.setattr(service_install, "_macos_plist_path", lambda: plist)
    monkeypatch.setattr(service_install.os, "getuid", lambda: 501)
    run_mock = MagicMock(return_value=_ok())
    monkeypatch.setattr(service_install, "_run", run_mock)
    service_install.uninstall_macos()
    assert not plist.exists()
    assert run_mock.call_args.args[0][:3] == ["launchctl", "bootout", "gui/501/com.grezy.autoclaude"]


def test_install_dispatches_by_platform(monkeypatch) -> None:
    sentinel = service_install.InstallResult(platform="darwin", action="installed", detail="x")
    monkeypatch.setattr(service_install.sys, "platform", "darwin")
    monkeypatch.setattr(service_install, "install_macos", lambda *_args, **_kwargs: sentinel)
    monkeypatch.setattr(service_install, "_resolve_autoclaude_binary", lambda: "/x")
    assert service_install.install("default") is sentinel


def test_install_unsupported_platform_raises(monkeypatch) -> None:
    monkeypatch.setattr(service_install.sys, "platform", "freebsd13")
    monkeypatch.setattr(service_install, "_resolve_autoclaude_binary", lambda: "/x")
    with pytest.raises(ServiceInstallError, match="freebsd13"):
        service_install.install("default")


def test_resolve_autoclaude_binary_prefers_env_override(monkeypatch) -> None:
    monkeypatch.setenv("AUTOCLAUDE_BINARY", "/opt/bin/autoclaude")
    assert service_install._resolve_autoclaude_binary() == "/opt/bin/autoclaude"  # noqa: SLF001


def test_resolve_autoclaude_binary_falls_back_to_module(monkeypatch) -> None:
    monkeypatch.delenv("AUTOCLAUDE_BINARY", raising=False)
    with patch.object(service_install.shutil, "which", return_value=None):
        result = service_install._resolve_autoclaude_binary()  # noqa: SLF001
    assert result.endswith("-m autoclaude.cli")
