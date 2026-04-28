"""CLI version freshness check driven by the daemon heartbeat response.

The server returns ``latest_version`` (newest released CLI on PyPI) and
``min_version`` (oldest still-accepted CLI) on every daemon heartbeat. We
persist that into ``config_dir()/update_check.json`` so:

- The headless daemon fires a native desktop notification when newer.
- Any foreground ``autoclaude`` command prints a rich panel notice.
- Both contexts hard-stop with exit code 2 when the local version is below
  ``min_version`` -- the server has changed the wire contract and the user
  must upgrade before the CLI can continue.

Throttling: notifications fire at most once per ``latest_version`` value via
the ``last_notified_version`` field; bumping the constant on the server is
what causes a re-notify.

Dev ergonomics:
- ``AUTOCLAUDE_FORCE_LATEST`` / ``AUTOCLAUDE_FORCE_MIN`` env vars override the
  server-supplied values so a developer can rehearse the upgrade path locally
  without touching the backend.
- ``autoclaude update-check`` (in ``cli.py``) inspects + clears the persisted
  state.
"""

from __future__ import annotations

import contextlib
import json
import os
import platform
import shutil
import subprocess
import time
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from autoclaude import __version__
from autoclaude.config import config_dir
from autoclaude.logger import get_logger

_log = get_logger("update_check")

STATE_FILENAME = "update_check.json"
UPGRADE_HINT = "uv tool upgrade autoclaude-cli  (or: pipx upgrade autoclaude-cli)"


def state_path() -> Path:
    return config_dir() / STATE_FILENAME


@dataclass
class UpdateState:
    """On-disk record of the latest heartbeat-reported version constraints.

    ``last_notified_version`` is updated only after a notification is fired,
    so re-running the daemon after a notice does not respam the user.
    """

    latest_version: str = ""
    min_version: str = ""
    checked_at: str = ""
    last_notified_version: str = ""
    last_blocking_version: str = ""

    @classmethod
    def load(cls) -> UpdateState:
        path = state_path()
        if not path.exists():
            return cls()
        try:
            data = json.loads(path.read_text())
        except (OSError, ValueError, json.JSONDecodeError):
            return cls()
        return cls(
            latest_version=str(data.get("latest_version") or ""),
            min_version=str(data.get("min_version") or ""),
            checked_at=str(data.get("checked_at") or ""),
            last_notified_version=str(data.get("last_notified_version") or ""),
            last_blocking_version=str(data.get("last_blocking_version") or ""),
        )

    def save(self) -> None:
        path = state_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(asdict(self), indent=2))


@dataclass
class UpdateStatus:
    """Result of comparing the running CLI against server-supplied versions."""

    current: str
    latest: str
    minimum: str
    outdated: bool
    blocking: bool
    state: UpdateState


# ---------------------------------------------------------------------------
# Version comparison
# ---------------------------------------------------------------------------


def _parse_version(raw: str) -> tuple[int, ...]:
    """Best-effort PEP-440-ish parse: ``1.15.0+dev`` -> ``(1, 15, 0)``.

    Drops local segments after ``+``, prerelease suffixes (``a``/``b``/``rc``),
    and any non-numeric fragments. Returns ``(0,)`` for ``"0.0.0+dev"`` and
    other unparseable inputs so comparison stays well-defined.
    """
    if not raw:
        return (0,)
    head = raw.split("+", 1)[0].strip()
    parts: list[int] = []
    for chunk in head.split("."):
        digits = ""
        for ch in chunk:
            if ch.isdigit():
                digits += ch
            else:
                break
        if not digits:
            break
        parts.append(int(digits))
    return tuple(parts) if parts else (0,)


def _cmp_versions(a: str, b: str) -> int:
    pa, pb = _parse_version(a), _parse_version(b)
    if pa < pb:
        return -1
    if pa > pb:
        return 1
    return 0


def is_dev_build(version: str) -> bool:
    """``0.0.0+dev`` from an editable install -- skip the upgrade nag."""
    return "+dev" in version or version.startswith("0.0.0")


# ---------------------------------------------------------------------------
# Native notification (best-effort, swallows errors)
# ---------------------------------------------------------------------------


def _notify_macos(title: str, body: str) -> bool:
    osascript = shutil.which("osascript")
    if not osascript:
        return False
    script = f'display notification "{_escape_applescript(body)}" with title "{_escape_applescript(title)}"'
    with contextlib.suppress(OSError, subprocess.SubprocessError):
        subprocess.run([osascript, "-e", script], check=False, timeout=5)
        return True
    return False


def _notify_linux(title: str, body: str) -> bool:
    notify = shutil.which("notify-send")
    if not notify:
        return False
    with contextlib.suppress(OSError, subprocess.SubprocessError):
        subprocess.run([notify, title, body], check=False, timeout=5)
        return True
    return False


def _escape_applescript(text: str) -> str:
    return text.replace("\\", "\\\\").replace('"', '\\"')


def _native_notify(title: str, body: str) -> bool:
    system = platform.system().lower()
    if system == "darwin":
        return _notify_macos(title, body)
    if system == "linux":
        return _notify_linux(title, body)
    return False


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def current_version() -> str:
    return __version__


def apply_heartbeat_response(response: Any, *, current: str = "") -> UpdateStatus:
    """Persist the heartbeat-supplied versions and compute status.

    Tolerates a missing/non-dict response (older servers, network shape
    changes) by defaulting to empty strings -- the resulting status is then
    "not outdated, not blocking" and nothing happens.

    Honours the ``AUTOCLAUDE_FORCE_LATEST`` / ``AUTOCLAUDE_FORCE_MIN`` env
    vars so developers can rehearse the upgrade path without backend edits.
    """
    current_v = current or current_version()
    latest = ""
    minimum = ""
    if isinstance(response, dict):
        latest = str(response.get("latest_version") or "").strip()
        minimum = str(response.get("min_version") or "").strip()

    forced_latest = os.environ.get("AUTOCLAUDE_FORCE_LATEST", "").strip()
    forced_min = os.environ.get("AUTOCLAUDE_FORCE_MIN", "").strip()
    if forced_latest:
        latest = forced_latest
    if forced_min:
        minimum = forced_min

    state = UpdateState.load()
    state.latest_version = latest
    state.min_version = minimum
    state.checked_at = datetime.now(tz=UTC).isoformat()
    state.save()

    outdated = False
    blocking = False
    if not is_dev_build(current_v):
        if latest and _cmp_versions(current_v, latest) < 0:
            outdated = True
        if minimum and _cmp_versions(current_v, minimum) < 0:
            blocking = True

    return UpdateStatus(
        current=current_v,
        latest=latest,
        minimum=minimum,
        outdated=outdated,
        blocking=blocking,
        state=state,
    )


def maybe_notify(status: UpdateStatus) -> bool:
    """Fire a native notification once per latest-version value.

    Returns True iff a notification was actually dispatched (handy for tests
    and debug commands). Idempotent across daemon restarts.
    """
    if status.blocking:
        if status.state.last_blocking_version == status.minimum:
            return False
        title = "AutoClaude: upgrade required"
        body = f"CLI {status.current} is below the required minimum {status.minimum}. Run: {UPGRADE_HINT}"
        fired = _native_notify(title, body)
        status.state.last_blocking_version = status.minimum
        status.state.save()
        _log.warning(
            "blocking version detected current=%s min=%s notified=%s",
            status.current,
            status.minimum,
            fired,
            extra={"source": "cli"},
        )
        return fired

    if not status.outdated:
        return False
    if status.state.last_notified_version == status.latest:
        return False
    title = "AutoClaude: update available"
    body = f"{status.latest} is out (you have {status.current}). Run: {UPGRADE_HINT}"
    fired = _native_notify(title, body)
    status.state.last_notified_version = status.latest
    status.state.save()
    _log.info(
        "update available current=%s latest=%s notified=%s",
        status.current,
        status.latest,
        fired,
        extra={"source": "cli"},
    )
    return fired


def format_console_notice(status: UpdateStatus) -> str | None:
    """Return rich-markup notice for foreground commands, or ``None``.

    Caller is expected to print via the CLI logger (which renders rich markup).
    """
    if status.blocking:
        return (
            f"[bold red]AutoClaude requires >= {status.minimum}[/bold red] "
            f"(you have {status.current}). Run: [bold]{UPGRADE_HINT}[/bold]"
        )
    if status.outdated:
        return (
            f"[yellow]autoclaude {status.latest} available[/yellow] "
            f"(you have {status.current}). Run: [bold]{UPGRADE_HINT}[/bold]"
        )
    return None


def load_status() -> UpdateStatus:
    """Recompute status from the persisted state without hitting the network.

    Used by foreground CLI commands so they can surface the daemon-recorded
    versions without making their own heartbeat call.
    """
    state = UpdateState.load()
    current_v = current_version()
    outdated = False
    blocking = False
    if not is_dev_build(current_v):
        if state.latest_version and _cmp_versions(current_v, state.latest_version) < 0:
            outdated = True
        if state.min_version and _cmp_versions(current_v, state.min_version) < 0:
            blocking = True
    return UpdateStatus(
        current=current_v,
        latest=state.latest_version,
        minimum=state.min_version,
        outdated=outdated,
        blocking=blocking,
        state=state,
    )


def clear_state() -> bool:
    """Wipe the persisted state. Returns True if a file was removed."""
    path = state_path()
    if not path.exists():
        return False
    path.unlink()
    return True


def state_age_seconds() -> float | None:
    """Seconds since ``checked_at``, or None if state has never been written."""
    state = UpdateState.load()
    if not state.checked_at:
        return None
    try:
        ts = datetime.fromisoformat(state.checked_at).timestamp()
    except ValueError:
        return None
    return max(0.0, time.time() - ts)


__all__ = [
    "UPGRADE_HINT",
    "UpdateState",
    "UpdateStatus",
    "apply_heartbeat_response",
    "clear_state",
    "current_version",
    "format_console_notice",
    "is_dev_build",
    "load_status",
    "maybe_notify",
    "state_age_seconds",
    "state_path",
]
