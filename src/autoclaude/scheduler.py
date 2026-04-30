"""Periodic tick scheduler.

Sibling to the heartbeat daemon. Runs ``runner.run_tick`` on a fixed
interval (15 minutes minimum) so a logged-in user keeps draining the
server's tick queue without manual `autoclaude tick` invocations.

Independent service from the heartbeat: pausing the scheduler stops new
ticks from firing but keeps the heartbeat (and its task channel) alive.
"""

from __future__ import annotations

import contextlib
import signal
import threading
from typing import TYPE_CHECKING

from autoclaude.api_client import ApiError
from autoclaude.log_uploader import replay_pending
from autoclaude.logger import get_logger, profile_context
from autoclaude.runner import run_tick as runner_run_tick

if TYPE_CHECKING:
    from typing import Any

    from autoclaude.api_client import ApiClient

_log = get_logger("scheduler")

DEFAULT_INTERVAL_SECONDS = 15 * 60
MIN_INTERVAL_SECONDS = 15 * 60
MAX_INTERVAL_SECONDS = 24 * 60 * 60


class Scheduler:
    """Run ticks on an interval. Single-threaded, lock-free.

    Accepts one or more ``ApiClient`` instances. On each cycle, every
    client ticks sequentially in registration order before the loop
    sleeps again. Overlap protection is implicit: the loop sleeps
    *after* the cycle returns, so a slow profile simply pushes the next
    start. The stop event is checked between profiles so SIGINT exits
    promptly even mid-cycle.
    """

    def __init__(
        self,
        clients: ApiClient | list[ApiClient],
        *,
        interval: float = DEFAULT_INTERVAL_SECONDS,
    ) -> None:
        self._clients: list[ApiClient] = [clients] if not isinstance(clients, list) else list(clients)
        if not self._clients:
            msg = "Scheduler requires at least one client"
            raise ValueError(msg)
        self._interval = max(MIN_INTERVAL_SECONDS, min(interval, MAX_INTERVAL_SECONDS))
        self._stop = threading.Event()

    def request_stop(self) -> None:
        self._stop.set()

    def run_forever(self) -> None:
        _log.info(
            "scheduler starting (interval=%ss, clients=%d)",
            self._interval,
            len(self._clients),
            extra={"source": "cli"},
        )
        while not self._stop.is_set():
            self._run_cycle()
            if self._stop.wait(self._interval):
                break
        _log.info("scheduler stopped", extra={"source": "cli"})

    def _run_cycle(self) -> None:
        for client in self._clients:
            if self._stop.is_set():
                return
            name = getattr(getattr(client, "profile", None), "name", None) or "?"
            with profile_context(name):
                self._run_one(client)

    def _run_one(self, client: ApiClient) -> None:
        with contextlib.suppress(Exception):
            replay_pending(client)
        try:
            exit_code = runner_run_tick(client)
        except ApiError as exc:
            _log.warning("scheduler tick api error: %s", exc, extra={"source": "cli"})
            return
        except Exception as exc:  # noqa: BLE001 (scheduler swallows everything to keep looping)
            _log.exception("scheduler tick crashed: %s", exc, extra={"source": "cli"})
            return
        if exit_code != 0:
            _log.warning("scheduler tick exited %s", exit_code, extra={"source": "cli"})


def _install_signal_handlers(scheduler: Scheduler) -> None:
    def _handle(_signum: int, _frame: Any) -> None:
        scheduler.request_stop()

    signal.signal(signal.SIGTERM, _handle)
    signal.signal(signal.SIGINT, _handle)


def run_scheduler(
    clients: ApiClient | list[ApiClient],
    *,
    interval: float = DEFAULT_INTERVAL_SECONDS,
) -> None:
    """Build and run a Scheduler in the foreground until SIGINT/SIGTERM."""
    scheduler = Scheduler(clients, interval=interval)
    _install_signal_handlers(scheduler)
    scheduler.run_forever()


__all__ = [
    "DEFAULT_INTERVAL_SECONDS",
    "MAX_INTERVAL_SECONDS",
    "MIN_INTERVAL_SECONDS",
    "Scheduler",
    "run_scheduler",
]
