"""Background heartbeat pinger for the duration of a tick.

Without this, ``_send_heartbeat`` only fires on step boundaries and during
``_run_lifecycle_step`` wraps. A multi-minute ``claude`` subprocess blocks
the main thread the whole time, so ``heartbeat_at`` on the server sits
unrefreshed and the dashboard flags the tick as ``stale — runner may be
stuck`` even though everything is fine.

``HeartbeatPinger`` spawns one daemon thread that calls
``client.tick_heartbeat`` on a fixed interval. It's used as a context
manager so start/stop are paired with the tick's lifetime. Shipping the
latest running totals (tokens + cost) lets the UI show live progress
instead of the last-step-close snapshot.
"""

from __future__ import annotations

import threading
from typing import TYPE_CHECKING, Self

from autoclaude.api_client import ApiError
from autoclaude.logger import get_logger

if TYPE_CHECKING:
    from collections.abc import Callable

    from autoclaude.api_client import ApiClient

_log = get_logger("heartbeat")

# Frontend flips to "stale" past 60s (see `current-tick-card.tsx`).
# 20s gives three pings per stale-window -- enough headroom that one
# dropped request does not tip the UI red.
DEFAULT_INTERVAL_SECONDS = 20.0


class HeartbeatPinger:
    """Send ``tick_heartbeat`` on a timer for the duration of a tick."""

    def __init__(
        self,
        client: ApiClient,
        tick_id: int,
        *,
        get_totals: Callable[[], tuple[int, float]],
        interval: float = DEFAULT_INTERVAL_SECONDS,
    ) -> None:
        self._client = client
        self._tick_id = tick_id
        self._get_totals = get_totals
        self._interval = interval
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def __enter__(self) -> Self:
        self.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.stop()

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._run,
            name=f"autoclaude-heartbeat-{self._tick_id}",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=self._interval + 1.0)
            self._thread = None

    def _run(self) -> None:
        # `Event.wait` returns True when `stop()` fires, False on timeout; the
        # loop ends on True so shutdown is immediate rather than waiting out
        # the current interval.
        while not self._stop.wait(self._interval):
            tokens, cost = self._safe_totals()
            try:
                self._client.tick_heartbeat(
                    self._tick_id,
                    token_cost_estimate=tokens,
                    cost_usd=cost,
                )
            except ApiError as exc:
                # Logged but not retried: if the backend is unreachable a
                # dropped ping is no worse than the status quo, and the next
                # interval will try again.
                _log.warning("background heartbeat failed: %s", exc, extra={"source": "cli"})

    def _safe_totals(self) -> tuple[int, float]:
        """Swallow getter failures so a bug there cannot kill the pinger."""
        try:
            return self._get_totals()
        except Exception as exc:  # noqa: BLE001
            _log.warning("heartbeat totals getter raised: %s", exc, extra={"source": "cli"})
            return 0, 0.0


__all__ = ["DEFAULT_INTERVAL_SECONDS", "HeartbeatPinger"]
