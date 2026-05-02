"""Tests for the credentials watcher install/uninstall/status flow."""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from autoclaude import creds_watcher


def _ok(stdout: str = "", stderr: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=[], returncode=0, stdout=stdout, stderr=stderr)


def _fail(stdout: str = "", stderr: str = "boom") -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(args=[], returncode=1, stdout=stdout, stderr=stderr)


@pytest.fixture()
def _linux(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(creds_watcher.sys, "platform", "linux")


@pytest.fixture()
def _has_inotify(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pretend ``inotifywait`` is on PATH so install does not try to apt-get."""
    monkeypatch.setattr(creds_watcher.shutil, "which", lambda name: f"/usr/bin/{name}")


def test_install_skips_on_non_linux(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(creds_watcher.sys, "platform", "darwin")
    result = creds_watcher.install_watcher()
    assert result.action == "skipped"
    assert "darwin" in result.detail


def test_uninstall_skips_on_non_linux(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(creds_watcher.sys, "platform", "win32")
    result = creds_watcher.uninstall_watcher()
    assert result.action == "skipped"


def test_watcher_status_unsupported_off_linux(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(creds_watcher.sys, "platform", "darwin")
    assert creds_watcher.watcher_status() == "unsupported"


def test_install_writes_unit_and_script_and_enables(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    _linux: None,
    _has_inotify: None,
) -> None:
    unit_path = tmp_path / "unit" / "autoclaude-creds-watcher.service"
    script_path = tmp_path / "bin" / "creds-watcher.sh"
    monkeypatch.setattr(creds_watcher, "_systemd_unit_path", lambda: unit_path)
    monkeypatch.setattr(creds_watcher, "_watcher_script_path", lambda: script_path)
    systemctl = MagicMock(return_value=_ok())
    monkeypatch.setattr(creds_watcher, "_systemctl", systemctl)

    result = creds_watcher.install_watcher(group="autoclaude", home=tmp_path)

    assert result.action == "installed"
    assert result.detail == str(unit_path)
    assert script_path.exists()
    assert script_path.stat().st_mode & 0o111, "script must be executable"
    script_body = script_path.read_text()
    assert "inotifywait" in script_body
    assert "chgrp" in script_body
    assert "chmod g+r" in script_body
    assert ".credentials.json" in script_body
    unit_body = unit_path.read_text()
    assert "ExecStart=" in unit_body
    assert str(script_path) in unit_body
    cmds = [call.args[0] for call in systemctl.call_args_list]
    assert ["daemon-reload"] in cmds
    assert ["enable", "--now", creds_watcher.WATCHER_SYSTEMD_UNIT] in cmds


def test_install_raises_on_systemctl_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    _linux: None,
    _has_inotify: None,
) -> None:
    monkeypatch.setattr(creds_watcher, "_systemd_unit_path", lambda: tmp_path / "unit.service")
    monkeypatch.setattr(creds_watcher, "_watcher_script_path", lambda: tmp_path / "watch.sh")
    monkeypatch.setattr(
        creds_watcher,
        "_systemctl",
        MagicMock(side_effect=[_ok(), _fail(stderr="enable rejected")]),
    )

    with pytest.raises(creds_watcher.CredsWatcherError, match="enable rejected"):
        creds_watcher.install_watcher(group="autoclaude", home=tmp_path)


def test_install_skips_when_inotifywait_missing_and_no_apt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    _linux: None,
) -> None:
    """Without inotifywait or apt-get, install must skip cleanly with a hint."""
    monkeypatch.setattr(creds_watcher.shutil, "which", lambda _name: None)
    systemctl = MagicMock(return_value=_ok())
    monkeypatch.setattr(creds_watcher, "_systemctl", systemctl)

    result = creds_watcher.install_watcher(group="autoclaude", home=tmp_path)

    assert result.action == "skipped"
    assert "inotify-tools" in result.detail
    systemctl.assert_not_called()


def test_uninstall_disables_and_removes_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    _linux: None,
) -> None:
    unit_path = tmp_path / "autoclaude-creds-watcher.service"
    script_path = tmp_path / "creds-watcher.sh"
    unit_path.write_text("# stub")
    script_path.write_text("# stub")
    monkeypatch.setattr(creds_watcher, "_systemd_unit_path", lambda: unit_path)
    monkeypatch.setattr(creds_watcher, "_watcher_script_path", lambda: script_path)
    systemctl = MagicMock(return_value=_ok())
    monkeypatch.setattr(creds_watcher, "_systemctl", systemctl)

    result = creds_watcher.uninstall_watcher()

    assert result.action == "uninstalled"
    assert not unit_path.exists()
    assert not script_path.exists()
    cmds = [call.args[0] for call in systemctl.call_args_list]
    assert ["disable", "--now", creds_watcher.WATCHER_SYSTEMD_UNIT] in cmds
    assert ["daemon-reload"] in cmds


def test_uninstall_is_idempotent_when_files_absent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    _linux: None,
) -> None:
    monkeypatch.setattr(creds_watcher, "_systemd_unit_path", lambda: tmp_path / "missing.service")
    monkeypatch.setattr(creds_watcher, "_watcher_script_path", lambda: tmp_path / "missing.sh")
    monkeypatch.setattr(creds_watcher, "_systemctl", MagicMock(return_value=_ok()))

    # Must not raise.
    result = creds_watcher.uninstall_watcher()
    assert result.action == "uninstalled"


def test_watcher_status_not_installed_when_unit_missing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    _linux: None,
) -> None:
    monkeypatch.setattr(creds_watcher, "_systemd_unit_path", lambda: tmp_path / "absent.service")
    assert creds_watcher.watcher_status() == "not_installed"


def test_watcher_status_returns_systemctl_output_when_installed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    _linux: None,
) -> None:
    unit_path = tmp_path / "autoclaude-creds-watcher.service"
    unit_path.write_text("# stub")
    monkeypatch.setattr(creds_watcher, "_systemd_unit_path", lambda: unit_path)
    monkeypatch.setattr(creds_watcher, "_systemctl", MagicMock(return_value=_ok(stdout="active\n")))
    assert creds_watcher.watcher_status() == "active"

    monkeypatch.setattr(creds_watcher, "_systemctl", MagicMock(return_value=_fail(stdout="inactive\n")))
    assert creds_watcher.watcher_status() == "inactive"
