"""Long-running background process: heartbeat the server every ~30s.

Independent of the per-tick :class:`HeartbeatPinger`; this is the always-on
liveness signal that powers the dashboard's "Active CLIs" KPI. Any tick
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
from autoclaude.logger import get_logger, profile_context
from autoclaude.update_check import apply_heartbeat_response, maybe_notify
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
    """Heartbeat loop tied to one or more ``ApiClient`` instances.

    On each cycle, every client heartbeats sequentially in registration
    order. Per-client throttles (claude_usage shipping) are tracked
    independently so adding a profile does not change shipping cadence
    for the others.
    """

    def __init__(
        self,
        clients: ApiClient | list[ApiClient],
        *,
        cli_version: str = "",
        interval: float = DEFAULT_INTERVAL_SECONDS,
        identity: InstallationIdentity | None = None,
    ) -> None:
        self._clients: list[ApiClient] = [clients] if not isinstance(clients, list) else list(clients)
        if not self._clients:
            msg = "Daemon requires at least one client"
            raise ValueError(msg)
        self._cli_version = cli_version
        self._interval = max(MIN_INTERVAL_SECONDS, min(interval, MAX_INTERVAL_SECONDS))
        self._identity = identity or get_or_create_identity()
        self._stop = threading.Event()
        # Per-client monotonic timestamp of the last claude_usage shipment.
        # Tracked per-client so each server gets one sample per
        # CLAUDE_USAGE_INTERVAL_SECONDS independently.
        self._last_usage_sent_at: dict[int, float] = {id(c): 0.0 for c in self._clients}

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
            "daemon starting (installation=%s host=%s os=%s interval=%ss clients=%d)",
            self._identity.installation_id,
            self._identity.hostname,
            self._identity.os_platform,
            self._interval,
            len(self._clients),
            extra={"source": "cli"},
        )
        while not self._stop.is_set():
            for client in self._clients:
                if self._stop.is_set():
                    break
                with profile_context(client.profile.name):
                    self._tick_once(client)
            if self._stop.wait(self._interval):
                break
        _log.info("daemon stopped", extra={"source": "cli"})

    def _tick_once(self, client: ApiClient) -> None:
        claude_usage = self._maybe_collect_claude_usage(client)
        try:
            response = client.heartbeat(
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
            self._last_usage_sent_at[id(client)] = time.monotonic()

        # Surface CLI version freshness via native notification + persisted state.
        # The foreground CLI reads the same state file to render an in-terminal
        # notice; below `min_version` we hard-stop so launchd marks the service
        # failed and the user is forced to upgrade.
        status = apply_heartbeat_response(response, current=self._cli_version)
        maybe_notify(status)
        if status.blocking:
            _log.error(
                "[red]autoclaude %s is below required minimum %s; daemon stopping. "
                "Upgrade with: uv tool upgrade autoclaude-cli[/red]",
                status.current,
                status.minimum,
                extra={"source": "cli"},
            )
            self._stop.set()
            raise SystemExit(2)

        next_interval = response.get("next_heartbeat_in_seconds") if isinstance(response, dict) else None
        if isinstance(next_interval, (int, float)) and next_interval > 0:
            self._interval = max(MIN_INTERVAL_SECONDS, min(float(next_interval), MAX_INTERVAL_SECONDS))

    def _maybe_collect_claude_usage(self, client: ApiClient) -> dict | None:
        """Read the latest cached rate_limits sample if it's this client's turn to ship.

        Throttles per-client to ``CLAUDE_USAGE_INTERVAL_SECONDS`` so a
        30-second daemon cadence does not flood any single server with
        duplicate rows. Returns None when the sample is missing, stale,
        or this client shipped one recently.
        """
        last = self._last_usage_sent_at.get(id(client), 0.0)
        elapsed = time.monotonic() - last
        if last > 0 and elapsed < CLAUDE_USAGE_INTERVAL_SECONDS:
            return None
        return read_latest_usage(max_age_seconds=CLAUDE_USAGE_MAX_AGE_SECONDS)


def _install_signal_handlers(daemon: Daemon) -> None:
    def _handle(_signum: int, _frame: Any) -> None:
        daemon.request_stop()

    signal.signal(signal.SIGTERM, _handle)
    signal.signal(signal.SIGINT, _handle)


def run_daemon(
    clients: ApiClient | list[ApiClient],
    *,
    cli_version: str = "",
    interval: float = DEFAULT_INTERVAL_SECONDS,
) -> None:
    """Build and run a Daemon in the foreground until SIGINT/SIGTERM."""
    daemon = Daemon(clients, cli_version=cli_version, interval=interval)
    _install_signal_handlers(daemon)
    daemon.run_forever()


__all__ = [
    "DEFAULT_INTERVAL_SECONDS",
    "Daemon",
    "run_daemon",
]
