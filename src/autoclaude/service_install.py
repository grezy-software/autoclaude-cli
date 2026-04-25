"""Per-user service registration for the AutoClaude daemon.

Three platforms are supported:

- macOS: a launchd LaunchAgent at ``~/Library/LaunchAgents/<label>.plist``,
  loaded with ``launchctl bootstrap gui/<uid>``.
- Linux: a ``systemd --user`` unit at
  ``~/.config/systemd/user/autoclaude.service``, enabled with
  ``systemctl --user enable --now``.
- Windows: a Task Scheduler entry created at user logon via ``schtasks.exe``.

All three end up running ``<autoclaude> daemon --profile <name>`` in the
foreground; the platform handles restart/respawn semantics.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

LAUNCHD_LABEL = "com.grezy.autoclaude"
SYSTEMD_UNIT_NAME = "autoclaude.service"
SCHTASKS_NAME = "AutoClaude"


@dataclass(frozen=True)
class InstallResult:
    platform: str
    action: str
    detail: str


class ServiceInstallError(RuntimeError):
    """Raised when the service registration cannot complete."""


def _resolve_autoclaude_binary() -> str:
    """Find the ``autoclaude`` entry point that the service should invoke.

    Prefer an explicit ``$AUTOCLAUDE_BINARY`` (lets users override for
    development), then ``autoclaude`` on ``PATH``, and finally fall back to
    ``<sys.executable> -m autoclaude.cli`` so a freshly-installed package
    still starts even if the launcher script is not on ``PATH`` yet.
    """
    override = os.environ.get("AUTOCLAUDE_BINARY", "").strip()
    if override:
        return override
    found = shutil.which("autoclaude")
    if found:
        return found
    return f"{sys.executable} -m autoclaude.cli"


def _macos_plist_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{LAUNCHD_LABEL}.plist"


def _systemd_unit_path() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "systemd" / "user" / SYSTEMD_UNIT_NAME


def _macos_plist(binary: str, profile: str) -> str:
    program_args = "\n".join(
        f"        <string>{piece}</string>" for piece in [*binary.split(), "daemon", "--profile", profile]
    )
    return f"""<?xml version=\"1.0\" encoding=\"UTF-8\"?>
<!DOCTYPE plist PUBLIC \"-//Apple//DTD PLIST 1.0//EN\" \"http://www.apple.com/DTDs/PropertyList-1.0.dtd\">
<plist version=\"1.0\">
<dict>
    <key>Label</key>
    <string>{LAUNCHD_LABEL}</string>
    <key>ProgramArguments</key>
    <array>
{program_args}
    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>StandardOutPath</key>
    <string>{Path.home() / ".config" / "autoclaude" / "logs" / "daemon.out.log"}</string>
    <key>StandardErrorPath</key>
    <string>{Path.home() / ".config" / "autoclaude" / "logs" / "daemon.err.log"}</string>
</dict>
</plist>
"""


def _systemd_unit(binary: str, profile: str) -> str:
    return f"""[Unit]
Description=AutoClaude background daemon
After=network-online.target

[Service]
Type=simple
ExecStart={binary} daemon --profile {profile}
Restart=on-failure
RestartSec=10
StandardOutput=append:%h/.config/autoclaude/logs/daemon.out.log
StandardError=append:%h/.config/autoclaude/logs/daemon.err.log

[Install]
WantedBy=default.target
"""


def _run(cmd: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, check=False, capture_output=True, text=True)


def install_macos(binary: str, profile: str) -> InstallResult:
    plist_path = _macos_plist_path()
    plist_path.parent.mkdir(parents=True, exist_ok=True)
    plist_path.write_text(_macos_plist(binary, profile))
    uid = os.getuid()
    # bootstrap is the modern equivalent of `launchctl load`. If a stale
    # entry exists we bootout first so install is idempotent.
    _run(["launchctl", "bootout", f"gui/{uid}/{LAUNCHD_LABEL}"])
    result = _run(["launchctl", "bootstrap", f"gui/{uid}", str(plist_path)])
    if result.returncode != 0:
        msg = f"launchctl bootstrap failed: {result.stderr.strip() or result.stdout.strip()}"
        raise ServiceInstallError(msg)
    return InstallResult(platform="darwin", action="installed", detail=str(plist_path))


def uninstall_macos() -> InstallResult:
    plist_path = _macos_plist_path()
    uid = os.getuid()
    _run(["launchctl", "bootout", f"gui/{uid}/{LAUNCHD_LABEL}"])
    if plist_path.exists():
        plist_path.unlink()
    return InstallResult(platform="darwin", action="uninstalled", detail=str(plist_path))


def status_macos() -> InstallResult:
    uid = os.getuid()
    result = _run(["launchctl", "print", f"gui/{uid}/{LAUNCHD_LABEL}"])
    return InstallResult(
        platform="darwin",
        action="status",
        detail=("running" if result.returncode == 0 else "not_loaded"),
    )


def install_linux(binary: str, profile: str) -> InstallResult:
    unit_path = _systemd_unit_path()
    unit_path.parent.mkdir(parents=True, exist_ok=True)
    unit_path.write_text(_systemd_unit(binary, profile))
    _run(["systemctl", "--user", "daemon-reload"])
    result = _run(["systemctl", "--user", "enable", "--now", SYSTEMD_UNIT_NAME])
    if result.returncode != 0:
        msg = f"systemctl --user enable --now failed: {result.stderr.strip() or result.stdout.strip()}"
        raise ServiceInstallError(msg)
    return InstallResult(platform="linux", action="installed", detail=str(unit_path))


def uninstall_linux() -> InstallResult:
    unit_path = _systemd_unit_path()
    _run(["systemctl", "--user", "disable", "--now", SYSTEMD_UNIT_NAME])
    if unit_path.exists():
        unit_path.unlink()
    _run(["systemctl", "--user", "daemon-reload"])
    return InstallResult(platform="linux", action="uninstalled", detail=str(unit_path))


def status_linux() -> InstallResult:
    result = _run(["systemctl", "--user", "is-active", SYSTEMD_UNIT_NAME])
    return InstallResult(
        platform="linux",
        action="status",
        detail=(result.stdout.strip() or "unknown"),
    )


def install_windows(binary: str, profile: str) -> InstallResult:
    cmd = [
        "schtasks.exe",
        "/Create",
        "/F",
        "/SC",
        "ONLOGON",
        "/RL",
        "LIMITED",
        "/TN",
        SCHTASKS_NAME,
        "/TR",
        f'"{binary}" daemon --profile {profile}',
    ]
    result = _run(cmd)
    if result.returncode != 0:
        msg = f"schtasks /Create failed: {result.stderr.strip() or result.stdout.strip()}"
        raise ServiceInstallError(msg)
    _run(["schtasks.exe", "/Run", "/TN", SCHTASKS_NAME])
    return InstallResult(platform="win32", action="installed", detail=SCHTASKS_NAME)


def uninstall_windows() -> InstallResult:
    _run(["schtasks.exe", "/End", "/TN", SCHTASKS_NAME])
    _run(["schtasks.exe", "/Delete", "/F", "/TN", SCHTASKS_NAME])
    return InstallResult(platform="win32", action="uninstalled", detail=SCHTASKS_NAME)


def status_windows() -> InstallResult:
    result = _run(["schtasks.exe", "/Query", "/TN", SCHTASKS_NAME])
    return InstallResult(
        platform="win32",
        action="status",
        detail=("registered" if result.returncode == 0 else "not_registered"),
    )


def install(profile: str) -> InstallResult:
    """Register the daemon as a per-user service for the current platform."""
    binary = _resolve_autoclaude_binary()
    if sys.platform == "darwin":
        return install_macos(binary, profile)
    if sys.platform.startswith("linux"):
        return install_linux(binary, profile)
    if sys.platform.startswith("win"):
        return install_windows(binary, profile)
    msg = f"unsupported platform: {sys.platform}"
    raise ServiceInstallError(msg)


def uninstall() -> InstallResult:
    if sys.platform == "darwin":
        return uninstall_macos()
    if sys.platform.startswith("linux"):
        return uninstall_linux()
    if sys.platform.startswith("win"):
        return uninstall_windows()
    msg = f"unsupported platform: {sys.platform}"
    raise ServiceInstallError(msg)


def status() -> InstallResult:
    if sys.platform == "darwin":
        return status_macos()
    if sys.platform.startswith("linux"):
        return status_linux()
    if sys.platform.startswith("win"):
        return status_windows()
    msg = f"unsupported platform: {sys.platform}"
    raise ServiceInstallError(msg)


__all__ = [
    "LAUNCHD_LABEL",
    "SCHTASKS_NAME",
    "SYSTEMD_UNIT_NAME",
    "InstallResult",
    "ServiceInstallError",
    "install",
    "status",
    "uninstall",
]
