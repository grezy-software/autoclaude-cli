"""Persistent re-application of group access on the Claude credentials file.

``claude`` rotates ``~/.claude/.credentials.json`` on token refresh and
writes the new file as ``mode 0600`` owned by ``root:root``. This silently
breaks the ``autoclaude`` user's read access mid-tick (between two
``claude -p`` subprocesses inside the same tick), even though
:func:`claude_env.share_claude_credentials` re-applies group perms before
each subprocess.

This module installs a small inotify-driven systemd unit that watches
``~/.claude/`` and re-applies ``chgrp autoclaude`` + ``chmod g+r`` on
``.credentials.json`` whenever it is created, written, or atomically
renamed onto. The race window between claude's rewrite and the watcher's
reaction is on the order of milliseconds and is acceptable in practice.

Linux-only: the privilege-drop via ``runuser`` only happens on Linux.
macOS and Windows install/uninstall calls are no-ops that return a
``"skipped"`` result.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import textwrap
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from autoclaude import claude_env
from autoclaude.logger import get_logger

_log = get_logger("creds_watcher")

WATCHER_SYSTEMD_UNIT: Final[str] = "autoclaude-creds-watcher.service"


@dataclass(frozen=True)
class WatcherInstallResult:
    """Outcome of an install/uninstall call.

    Mirrors :class:`service_install.InstallResult` so callers can render
    both with the same logging template.
    """

    action: str
    detail: str


class CredsWatcherError(RuntimeError):
    """Raised when the watcher cannot be installed/uninstalled."""


def _is_linux() -> bool:
    return sys.platform.startswith("linux")


def _systemd_unit_path() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "systemd" / "user" / WATCHER_SYSTEMD_UNIT


def _watcher_script_path() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(base) / "autoclaude" / "bin" / "creds-watcher.sh"


def _watcher_script_body(*, creds_file: Path, group: str) -> str:
    return textwrap.dedent(f"""\
        #!/usr/bin/env bash
        # autoclaude credentials permission watcher.
        # Re-applies `chgrp {group}` + `chmod g+r` on {creds_file} whenever
        # `claude` rotates it, so the {group} user keeps read access.
        set -euo pipefail

        CREDS_FILE="${{CREDS_FILE:-{creds_file}}}"
        GROUP="${{GROUP:-{group}}}"
        WATCH_DIR="$(dirname "$CREDS_FILE")"
        WATCH_NAME="$(basename "$CREDS_FILE")"

        apply_perms() {{
            [[ -e "$CREDS_FILE" ]] || return 0
            chgrp "$GROUP" "$CREDS_FILE" 2>/dev/null || true
            chmod g+r "$CREDS_FILE" 2>/dev/null || true
        }}

        # Apply once on startup so we recover from a refresh that happened
        # while the watcher was down.
        apply_perms

        # close_write: claude wrote then closed the file in place.
        # moved_to:    a temp file was atomically renamed onto our path.
        # create:      the file was recreated from scratch.
        exec inotifywait -m -e close_write,moved_to,create --format '%f' "$WATCH_DIR" \\
            | while read -r changed; do
                if [[ "$changed" == "$WATCH_NAME" ]]; then
                    apply_perms
                fi
            done
        """)


def _systemd_unit_body(*, script_path: Path) -> str:
    return textwrap.dedent(f"""\
        [Unit]
        Description=AutoClaude credentials permission watcher
        After=local-fs.target

        [Service]
        Type=simple
        ExecStart={script_path}
        Restart=on-failure
        RestartSec=10
        StandardOutput=append:%h/.config/autoclaude/logs/creds-watcher.out.log
        StandardError=append:%h/.config/autoclaude/logs/creds-watcher.err.log

        [Install]
        WantedBy=default.target
        """)


def _systemctl(args: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["systemctl", "--user", *args],
        check=False,
        capture_output=True,
        text=True,
    )


def _ensure_inotifywait() -> bool:
    """Return ``True`` iff ``inotifywait`` is available (best-effort apt-install).

    The watcher script depends on ``inotifywait`` from the ``inotify-tools``
    package. On Debian/Ubuntu hosts running as root we transparently install
    it; everywhere else we warn and let the operator handle it.
    """
    if shutil.which("inotifywait"):
        return True
    if not _is_linux():
        return False
    apt = shutil.which("apt-get")
    if not apt:
        _log.warning(
            "inotifywait is missing and apt-get is unavailable; install"
            " inotify-tools manually then re-run `autoclaude init --user-autoclaude`.",
        )
        return False
    _log.info("installing inotify-tools (required by the credentials watcher)...")
    result = subprocess.run(
        [apt, "install", "-y", "inotify-tools"],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        _log.warning(
            "apt-get install inotify-tools failed (rc=%s): %s",
            result.returncode,
            (result.stderr or result.stdout).strip(),
        )
        return False
    return shutil.which("inotifywait") is not None


def install_watcher(
    *,
    group: str = claude_env.AUTOCLAUDE_GROUP,
    home: Path | None = None,
) -> WatcherInstallResult:
    """Install the credentials watcher as a per-user systemd unit. Idempotent.

    On non-Linux platforms returns a ``"skipped"`` result without changes.
    On Linux but without ``inotifywait`` (and no apt-get), returns
    ``"skipped"`` with a remediation hint. Raises :class:`CredsWatcherError`
    when ``systemctl`` itself fails.
    """
    if not _is_linux():
        return WatcherInstallResult(action="skipped", detail=f"unsupported platform {sys.platform}")
    home = home if home is not None else Path.home()
    creds_file = home / ".claude" / claude_env.CREDENTIALS_FILENAME
    if not _ensure_inotifywait():
        return WatcherInstallResult(
            action="skipped",
            detail="inotifywait unavailable; install inotify-tools and re-run init",
        )

    script_path = _watcher_script_path()
    script_path.parent.mkdir(parents=True, exist_ok=True)
    script_path.write_text(_watcher_script_body(creds_file=creds_file, group=group))
    script_path.chmod(0o755)

    unit_path = _systemd_unit_path()
    unit_path.parent.mkdir(parents=True, exist_ok=True)
    unit_path.write_text(_systemd_unit_body(script_path=script_path))

    log_dir = home / ".config" / "autoclaude" / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)

    _systemctl(["daemon-reload"])
    result = _systemctl(["enable", "--now", WATCHER_SYSTEMD_UNIT])
    if result.returncode != 0:
        msg = (
            f"systemctl --user enable --now {WATCHER_SYSTEMD_UNIT} failed: "
            f"{(result.stderr or result.stdout).strip()}"
        )
        raise CredsWatcherError(msg)
    return WatcherInstallResult(action="installed", detail=str(unit_path))


def uninstall_watcher() -> WatcherInstallResult:
    """Disable and remove the watcher unit + script. Idempotent."""
    if not _is_linux():
        return WatcherInstallResult(action="skipped", detail=f"unsupported platform {sys.platform}")
    _systemctl(["disable", "--now", WATCHER_SYSTEMD_UNIT])
    unit_path = _systemd_unit_path()
    if unit_path.exists():
        unit_path.unlink()
    script_path = _watcher_script_path()
    if script_path.exists():
        script_path.unlink()
    _systemctl(["daemon-reload"])
    return WatcherInstallResult(action="uninstalled", detail=str(unit_path))


def watcher_status() -> str:
    """Return ``active`` / ``inactive`` / ``not_installed`` / ``unsupported``.

    Mirrors the format returned by :func:`service_install.status_service`
    so the diag output renders consistently across all autoclaude services.
    """
    if not _is_linux():
        return "unsupported"
    unit_path = _systemd_unit_path()
    if not unit_path.exists():
        return "not_installed"
    result = _systemctl(["is-active", WATCHER_SYSTEMD_UNIT])
    return result.stdout.strip() or "unknown"


__all__ = [
    "WATCHER_SYSTEMD_UNIT",
    "CredsWatcherError",
    "WatcherInstallResult",
    "install_watcher",
    "uninstall_watcher",
    "watcher_status",
]
