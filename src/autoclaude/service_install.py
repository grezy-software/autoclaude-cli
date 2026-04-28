"""Per-user service registration for AutoClaude.

Two services per logged-in profile:

- ``heartbeat`` -- always-on liveness daemon (``autoclaude daemon``).
- ``scheduler`` -- periodic tick runner (``autoclaude scheduler``).

Both are managed via the platform's per-user service supervisor:

- macOS: launchd LaunchAgent plists in ``~/Library/LaunchAgents/``.
- Linux: ``systemd --user`` units in ``~/.config/systemd/user/``.
- Windows: Task Scheduler entries created with ``schtasks.exe``.

The scheduler is the only pausable service: ``pause_scheduler`` disables
the unit so it does not auto-start on next login; ``play_scheduler``
re-enables and starts it. The heartbeat is never paused.

Legacy single-service installs (label ``com.grezy.autoclaude``) are
booted out on first install so upgrades from the older single-daemon
layout migrate cleanly.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

# New per-service identifiers.
HEARTBEAT_LABEL = "com.grezy.autoclaude.heartbeat"
SCHEDULER_LABEL = "com.grezy.autoclaude.scheduler"

HEARTBEAT_SYSTEMD_UNIT = "autoclaude-heartbeat.service"
SCHEDULER_SYSTEMD_UNIT = "autoclaude-scheduler.service"

HEARTBEAT_SCHTASKS_NAME = "AutoClaudeHeartbeat"
SCHEDULER_SCHTASKS_NAME = "AutoClaudeScheduler"

# Legacy single-service identifiers (pre-split). Cleaned up on install.
LEGACY_LAUNCHD_LABEL = "com.grezy.autoclaude"
LEGACY_SYSTEMD_UNIT = "autoclaude.service"
LEGACY_SCHTASKS_NAME = "AutoClaude"

ServiceKind = Literal["heartbeat", "scheduler"]


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


def _run(cmd: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, check=False, capture_output=True, text=True)


def _service_subcommand(kind: ServiceKind) -> str:
    return "daemon" if kind == "heartbeat" else "scheduler"


def _label(kind: ServiceKind) -> str:
    return HEARTBEAT_LABEL if kind == "heartbeat" else SCHEDULER_LABEL


def _systemd_unit(kind: ServiceKind) -> str:
    return HEARTBEAT_SYSTEMD_UNIT if kind == "heartbeat" else SCHEDULER_SYSTEMD_UNIT


def _schtasks_name(kind: ServiceKind) -> str:
    return HEARTBEAT_SCHTASKS_NAME if kind == "heartbeat" else SCHEDULER_SCHTASKS_NAME


# -- macOS / launchd ---------------------------------------------------------


def _macos_plist_path(kind: ServiceKind) -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{_label(kind)}.plist"


def _service_path() -> str:
    """PATH baked into launchd/systemd units so ``gh``/``claude``/``git`` resolve.

    launchd inherits a minimal PATH that excludes Homebrew and ``~/.local/bin``,
    so binaries the runner depends on are not found unless we declare them
    explicitly. The user's interactive PATH is captured at install time and
    extended with the well-known fallbacks.
    """
    pieces: list[str] = []
    seen: set[str] = set()
    extras = [
        os.environ.get("PATH", ""),
        f"{Path.home()}/.local/bin",
        "/opt/homebrew/bin",
        "/opt/homebrew/sbin",
        "/usr/local/bin",
        "/usr/bin",
        "/bin",
        "/usr/sbin",
        "/sbin",
    ]
    for part in extras:
        for raw in (part or "").split(":"):
            entry = raw.strip()
            if not entry or entry in seen:
                continue
            seen.add(entry)
            pieces.append(entry)
    return ":".join(pieces)


def _macos_plist(binary: str, profile: str, kind: ServiceKind) -> str:
    label = _label(kind)
    subcommand = _service_subcommand(kind)
    program_args = "\n".join(
        f"        <string>{piece}</string>"
        for piece in [*binary.split(), subcommand, "--profile", profile]
    )
    log_dir = Path.home() / ".config" / "autoclaude" / "logs"
    return f"""<?xml version=\"1.0\" encoding=\"UTF-8\"?>
<!DOCTYPE plist PUBLIC \"-//Apple//DTD PLIST 1.0//EN\" \"http://www.apple.com/DTDs/PropertyList-1.0.dtd\">
<plist version=\"1.0\">
<dict>
    <key>Label</key>
    <string>{label}</string>
    <key>ProgramArguments</key>
    <array>
{program_args}
    </array>
    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>{_service_path()}</string>
        <key>HOME</key>
        <string>{Path.home()}</string>
    </dict>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>StandardOutPath</key>
    <string>{log_dir / f"{kind}.out.log"}</string>
    <key>StandardErrorPath</key>
    <string>{log_dir / f"{kind}.err.log"}</string>
</dict>
</plist>
"""


def _macos_uid() -> int:
    return os.getuid()


def _macos_remove_legacy() -> None:
    """Remove pre-split single-service install if it lingers."""
    uid = _macos_uid()
    _run(["launchctl", "bootout", f"gui/{uid}/{LEGACY_LAUNCHD_LABEL}"])
    legacy_plist = Path.home() / "Library" / "LaunchAgents" / f"{LEGACY_LAUNCHD_LABEL}.plist"
    if legacy_plist.exists():
        legacy_plist.unlink()


def _macos_bootstrap(kind: ServiceKind, binary: str, profile: str) -> InstallResult:
    plist_path = _macos_plist_path(kind)
    plist_path.parent.mkdir(parents=True, exist_ok=True)
    plist_path.write_text(_macos_plist(binary, profile, kind))
    uid = _macos_uid()
    label = _label(kind)
    _run(["launchctl", "bootout", f"gui/{uid}/{label}"])
    _run(["launchctl", "enable", f"gui/{uid}/{label}"])
    result = _run(["launchctl", "bootstrap", f"gui/{uid}", str(plist_path)])
    if result.returncode != 0:
        msg = f"launchctl bootstrap failed: {result.stderr.strip() or result.stdout.strip()}"
        raise ServiceInstallError(msg)
    return InstallResult(platform="darwin", action="installed", detail=str(plist_path))


def _macos_bootout(kind: ServiceKind, *, remove_plist: bool) -> InstallResult:
    plist_path = _macos_plist_path(kind)
    uid = _macos_uid()
    label = _label(kind)
    _run(["launchctl", "bootout", f"gui/{uid}/{label}"])
    if remove_plist and plist_path.exists():
        plist_path.unlink()
    return InstallResult(platform="darwin", action="uninstalled", detail=str(plist_path))


def _macos_disable(kind: ServiceKind) -> None:
    uid = _macos_uid()
    label = _label(kind)
    _run(["launchctl", "disable", f"gui/{uid}/{label}"])
    _run(["launchctl", "bootout", f"gui/{uid}/{label}"])


def _macos_status(kind: ServiceKind) -> InstallResult:
    uid = _macos_uid()
    label = _label(kind)
    result = _run(["launchctl", "print", f"gui/{uid}/{label}"])
    return InstallResult(
        platform="darwin",
        action="status",
        detail=("running" if result.returncode == 0 else "not_loaded"),
    )


# -- Linux / systemd --------------------------------------------------------


def _systemd_unit_path(kind: ServiceKind) -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "systemd" / "user" / _systemd_unit(kind)


def _systemd_unit_body(binary: str, profile: str, kind: ServiceKind) -> str:
    subcommand = _service_subcommand(kind)
    return f"""[Unit]
Description=AutoClaude {kind} service
After=network-online.target

[Service]
Type=simple
Environment=PATH={_service_path()}
ExecStart={binary} {subcommand} --profile {profile}
Restart=on-failure
RestartSec=10
StandardOutput=append:%h/.config/autoclaude/logs/{kind}.out.log
StandardError=append:%h/.config/autoclaude/logs/{kind}.err.log

[Install]
WantedBy=default.target
"""


def _systemd_remove_legacy() -> None:
    _run(["systemctl", "--user", "disable", "--now", LEGACY_SYSTEMD_UNIT])
    legacy = Path.home() / ".config" / "systemd" / "user" / LEGACY_SYSTEMD_UNIT
    if legacy.exists():
        legacy.unlink()
    _run(["systemctl", "--user", "daemon-reload"])


def _systemd_install(kind: ServiceKind, binary: str, profile: str) -> InstallResult:
    unit_path = _systemd_unit_path(kind)
    unit_path.parent.mkdir(parents=True, exist_ok=True)
    unit_path.write_text(_systemd_unit_body(binary, profile, kind))
    _run(["systemctl", "--user", "daemon-reload"])
    result = _run(["systemctl", "--user", "enable", "--now", _systemd_unit(kind)])
    if result.returncode != 0:
        msg = f"systemctl --user enable --now failed: {result.stderr.strip() or result.stdout.strip()}"
        raise ServiceInstallError(msg)
    return InstallResult(platform="linux", action="installed", detail=str(unit_path))


def _systemd_uninstall(kind: ServiceKind) -> InstallResult:
    unit_path = _systemd_unit_path(kind)
    _run(["systemctl", "--user", "disable", "--now", _systemd_unit(kind)])
    if unit_path.exists():
        unit_path.unlink()
    _run(["systemctl", "--user", "daemon-reload"])
    return InstallResult(platform="linux", action="uninstalled", detail=str(unit_path))


def _systemd_status(kind: ServiceKind) -> InstallResult:
    result = _run(["systemctl", "--user", "is-active", _systemd_unit(kind)])
    return InstallResult(
        platform="linux",
        action="status",
        detail=(result.stdout.strip() or "unknown"),
    )


def _systemd_disable(kind: ServiceKind) -> None:
    _run(["systemctl", "--user", "disable", "--now", _systemd_unit(kind)])


def _systemd_enable(kind: ServiceKind) -> None:
    _run(["systemctl", "--user", "enable", "--now", _systemd_unit(kind)])


# -- Windows / schtasks -----------------------------------------------------


def _windows_install(kind: ServiceKind, binary: str, profile: str) -> InstallResult:
    name = _schtasks_name(kind)
    subcommand = _service_subcommand(kind)
    cmd = [
        "schtasks.exe",
        "/Create",
        "/F",
        "/SC",
        "ONLOGON",
        "/RL",
        "LIMITED",
        "/TN",
        name,
        "/TR",
        f'"{binary}" {subcommand} --profile {profile}',
    ]
    result = _run(cmd)
    if result.returncode != 0:
        msg = f"schtasks /Create failed: {result.stderr.strip() or result.stdout.strip()}"
        raise ServiceInstallError(msg)
    _run(["schtasks.exe", "/Run", "/TN", name])
    return InstallResult(platform="win32", action="installed", detail=name)


def _windows_uninstall(kind: ServiceKind) -> InstallResult:
    name = _schtasks_name(kind)
    _run(["schtasks.exe", "/End", "/TN", name])
    _run(["schtasks.exe", "/Delete", "/F", "/TN", name])
    return InstallResult(platform="win32", action="uninstalled", detail=name)


def _windows_status(kind: ServiceKind) -> InstallResult:
    name = _schtasks_name(kind)
    result = _run(["schtasks.exe", "/Query", "/TN", name])
    return InstallResult(
        platform="win32",
        action="status",
        detail=("registered" if result.returncode == 0 else "not_registered"),
    )


def _windows_disable(kind: ServiceKind) -> None:
    name = _schtasks_name(kind)
    _run(["schtasks.exe", "/End", "/TN", name])
    _run(["schtasks.exe", "/Change", "/TN", name, "/DISABLE"])


def _windows_enable(kind: ServiceKind) -> None:
    name = _schtasks_name(kind)
    _run(["schtasks.exe", "/Change", "/TN", name, "/ENABLE"])
    _run(["schtasks.exe", "/Run", "/TN", name])


def _windows_remove_legacy() -> None:
    _run(["schtasks.exe", "/End", "/TN", LEGACY_SCHTASKS_NAME])
    _run(["schtasks.exe", "/Delete", "/F", "/TN", LEGACY_SCHTASKS_NAME])


# -- Public dispatch --------------------------------------------------------


def _remove_legacy() -> None:
    if sys.platform == "darwin":
        _macos_remove_legacy()
    elif sys.platform.startswith("linux"):
        _systemd_remove_legacy()
    elif sys.platform.startswith("win"):
        _windows_remove_legacy()


def install_service(kind: ServiceKind, profile: str) -> InstallResult:
    binary = _resolve_autoclaude_binary()
    if sys.platform == "darwin":
        return _macos_bootstrap(kind, binary, profile)
    if sys.platform.startswith("linux"):
        return _systemd_install(kind, binary, profile)
    if sys.platform.startswith("win"):
        return _windows_install(kind, binary, profile)
    msg = f"unsupported platform: {sys.platform}"
    raise ServiceInstallError(msg)


def uninstall_service(kind: ServiceKind) -> InstallResult:
    if sys.platform == "darwin":
        return _macos_bootout(kind, remove_plist=True)
    if sys.platform.startswith("linux"):
        return _systemd_uninstall(kind)
    if sys.platform.startswith("win"):
        return _windows_uninstall(kind)
    msg = f"unsupported platform: {sys.platform}"
    raise ServiceInstallError(msg)


def status_service(kind: ServiceKind) -> InstallResult:
    if sys.platform == "darwin":
        return _macos_status(kind)
    if sys.platform.startswith("linux"):
        return _systemd_status(kind)
    if sys.platform.startswith("win"):
        return _windows_status(kind)
    msg = f"unsupported platform: {sys.platform}"
    raise ServiceInstallError(msg)


def install_all(profile: str) -> list[InstallResult]:
    """Install both heartbeat and scheduler services for ``profile``."""
    _remove_legacy()
    return [install_service("heartbeat", profile), install_service("scheduler", profile)]


def uninstall_all() -> list[InstallResult]:
    """Remove both services. Legacy single-service install is also cleaned up."""
    _remove_legacy()
    return [uninstall_service("heartbeat"), uninstall_service("scheduler")]


def pause_scheduler() -> InstallResult:
    """Stop and disable the scheduler so it does not auto-restart."""
    if sys.platform == "darwin":
        _macos_disable("scheduler")
        return InstallResult(platform="darwin", action="paused", detail=str(_macos_plist_path("scheduler")))
    if sys.platform.startswith("linux"):
        _systemd_disable("scheduler")
        return InstallResult(platform="linux", action="paused", detail=SCHEDULER_SYSTEMD_UNIT)
    if sys.platform.startswith("win"):
        _windows_disable("scheduler")
        return InstallResult(platform="win32", action="paused", detail=SCHEDULER_SCHTASKS_NAME)
    msg = f"unsupported platform: {sys.platform}"
    raise ServiceInstallError(msg)


def play_scheduler(profile: str) -> InstallResult:
    """(Re-)enable and start the scheduler. Reinstalls the unit if needed."""
    return install_service("scheduler", profile)


__all__ = [
    "HEARTBEAT_LABEL",
    "HEARTBEAT_SCHTASKS_NAME",
    "HEARTBEAT_SYSTEMD_UNIT",
    "SCHEDULER_LABEL",
    "SCHEDULER_SCHTASKS_NAME",
    "SCHEDULER_SYSTEMD_UNIT",
    "InstallResult",
    "ServiceInstallError",
    "ServiceKind",
    "install_all",
    "install_service",
    "pause_scheduler",
    "play_scheduler",
    "status_service",
    "uninstall_all",
    "uninstall_service",
]
