"""Long-running background process: heartbeat the server every ~30s.

Independent of the per-tick :class:`HeartbeatPinger`; this is the always-on
liveness signal that powers the dashboard's "Active CLIs" KPI and the small
task channel (debug file uploads, future programmatic chores). Any tick
that runs in parallel still emits its own ``tick_heartbeat`` separately.

Designed to be started by a per-user service (launchd / systemd / Task
Scheduler) and survive transient network failures: every loop iteration
swallows exceptions, logs them, and waits for the next interval.
"""

from __future__ import annotations

import signal
import threading
import time
from typing import TYPE_CHECKING

from autoclaude.api_client import ApiError
from autoclaude.installation import InstallationIdentity, get_or_create_identity
from autoclaude.logger import get_logger
from autoclaude.task_handlers import TASK_HANDLERS
from autoclaude.usage_capture import install_statusline, read_latest_usage

if TYPE_CHECKING:
    from typing import Any

    from autoclaude.api_client import ApiClient

_log = get_logger("daemon")

DEFAULT_INTERVAL_SECONDS = 30.0
MIN_INTERVAL_SECONDS = 5.0
MAX_INTERVAL_SECONDS = 600.0

# Cadence at which we ship Claude Code rate_limits to the server. Every
# 15 minutes is dense enough to draw a usable graph without spamming the
# DB or shipping data the user already saw via /usage.
CLAUDE_USAGE_INTERVAL_SECONDS = 15 * 60

# Refuse to ship a sample older than this -- the user might not have used
# Claude in the last day, in which case the cached value is meaningless.
CLAUDE_USAGE_MAX_AGE_SECONDS = 6 * 60 * 60


class Daemon:
    """One heartbeat loop tied to one ``ApiClient`` and one identity."""

    def __init__(
        self,
        client: ApiClient,
        *,
        cli_version: str = "",
        interval: float = DEFAULT_INTERVAL_SECONDS,
        identity: InstallationIdentity | None = None,
    ) -> None:
        self._client = client
        self._cli_version = cli_version
        self._interval = max(MIN_INTERVAL_SECONDS, min(interval, MAX_INTERVAL_SECONDS))
        self._identity = identity or get_or_create_identity()
        self._stop = threading.Event()
        # Monotonic timestamp of the last successful heartbeat that included a
        # claude_usage payload. Initialised to a sentinel that forces the next
        # heartbeat to ship usage if any sample is available.
        self._last_usage_sent_at: float = 0.0

    def request_stop(self) -> None:
        self._stop.set()

    def run_forever(self) -> None:
        """Block on the heartbeat loop until ``request_stop`` fires.

        Returns cleanly so the calling command can exit zero on graceful
        shutdown (e.g. SIGTERM from launchd).
        """
        statusline_state = install_statusline()
        _log.info("claude statusline install: %s", statusline_state, extra={"source": "cli"})
        _log.info(
            "daemon starting (installation=%s host=%s os=%s interval=%ss)",
            self._identity.installation_id,
            self._identity.hostname,
            self._identity.os_platform,
            self._interval,
            extra={"source": "cli"},
        )
        while not self._stop.is_set():
            self._tick_once()
            if self._stop.wait(self._interval):
                break
        _log.info("daemon stopped", extra={"source": "cli"})

    def _tick_once(self) -> None:
        claude_usage = self._maybe_collect_claude_usage()
        try:
            response = self._client.heartbeat(
                installation_id=self._identity.installation_id,
                hostname=self._identity.hostname,
                os_platform=self._identity.os_platform,
                cli_version=self._cli_version,
                claude_usage=claude_usage,
            )
        except ApiError as exc:
            _log.warning("heartbeat failed: %s", exc, extra={"source": "cli"})
            return
        if claude_usage is not None:
            self._last_usage_sent_at = time.monotonic()

        next_interval = response.get("next_heartbeat_in_seconds") if isinstance(response, dict) else None
        if isinstance(next_interval, (int, float)) and next_interval > 0:
            self._interval = max(MIN_INTERVAL_SECONDS, min(float(next_interval), MAX_INTERVAL_SECONDS))

        tasks = response.get("tasks") if isinstance(response, dict) else None
        if isinstance(tasks, list):
            for task in tasks:
                if not isinstance(task, dict):
                    continue
                self._dispatch_task(task)

    def _maybe_collect_claude_usage(self) -> dict | None:
        """Read the latest cached rate_limits sample if it's our turn to ship.

        Throttles to ``CLAUDE_USAGE_INTERVAL_SECONDS`` so a 30-second daemon
        cadence does not flood the server with duplicate rows. Returns None
        when the sample is missing, stale, or we sent one recently.
        """
        elapsed = time.monotonic() - self._last_usage_sent_at
        if self._last_usage_sent_at > 0 and elapsed < CLAUDE_USAGE_INTERVAL_SECONDS:
            return None
        return read_latest_usage(max_age_seconds=CLAUDE_USAGE_MAX_AGE_SECONDS)

    def _dispatch_task(self, task: dict[str, Any]) -> None:
        task_id = task.get("id")
        task_type = task.get("task_type") or ""
        payload = task.get("payload") or {}
        if not isinstance(task_id, int) or not isinstance(payload, dict):
            return
        handler = TASK_HANDLERS.get(task_type)
        if handler is None:
            self._report(task_id, status="failed", error_log=f"unknown task_type: {task_type}")
            return
        try:
            result = handler(self._client, payload)
        except Exception as exc:  # noqa: BLE001 (daemon swallows all handler errors)
            _log.exception("task %s (%s) failed: %s", task_id, task_type, exc, extra={"source": "cli"})
            self._report(task_id, status="failed", error_log=str(exc))
            return
        self._report(task_id, status="fulfilled", result=result if isinstance(result, dict) else {})

    def _report(
        self,
        task_id: int,
        *,
        status: str,
        result: dict[str, Any] | None = None,
        error_log: str = "",
    ) -> None:
        try:
            self._client.runner_task_complete(
                task_id,
                status=status,
                result=result or {},
                error_log=error_log,
            )
        except ApiError as exc:
            _log.warning(
                "runner_task_complete %s -> %s failed: %s",
                task_id,
                status,
                exc,
                extra={"source": "cli"},
            )


def _install_signal_handlers(daemon: Daemon) -> None:
    def _handle(_signum: int, _frame: Any) -> None:
        daemon.request_stop()

    signal.signal(signal.SIGTERM, _handle)
    signal.signal(signal.SIGINT, _handle)


def run_daemon(
    client: ApiClient,
    *,
    cli_version: str = "",
    interval: float = DEFAULT_INTERVAL_SECONDS,
) -> None:
    """Build and run a Daemon in the foreground until SIGINT/SIGTERM."""
    daemon = Daemon(client, cli_version=cli_version, interval=interval)
    _install_signal_handlers(daemon)
    daemon.run_forever()


__all__ = [
    "DEFAULT_INTERVAL_SECONDS",
    "Daemon",
    "run_daemon",
]
