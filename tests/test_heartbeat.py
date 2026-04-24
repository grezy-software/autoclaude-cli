"""Tests for the background heartbeat pinger."""

from __future__ import annotations

import threading
import time

import pytest

from autoclaude.api_client import ApiError
from autoclaude.heartbeat import HeartbeatPinger


class _FakeClient:
    """Captures every ``tick_heartbeat`` call + optional simulated failure."""

    def __init__(self, *, fail_on: set[int] | None = None) -> None:
        self.calls: list[tuple[int, int | None, float | None]] = []
        self._lock = threading.Lock()
        self._fail_on = fail_on or set()

    def tick_heartbeat(
        self,
        tick_id: int,
        *,
        token_cost_estimate: int | None = None,
        cost_usd: float | None = None,
    ) -> dict:
        with self._lock:
            call_idx = len(self.calls)
            self.calls.append((tick_id, token_cost_estimate, cost_usd))
        if call_idx in self._fail_on:
            msg = "simulated backend blip"
            raise ApiError(msg)
        return {"ok": True}


def _wait_for(predicate, timeout: float = 2.0, poll: float = 0.01) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(poll)
    pytest.fail(f"predicate never held within {timeout}s")


def test_heartbeat_pings_on_interval() -> None:
    client = _FakeClient()
    totals = [(0, 0.0), (10, 0.5), (25, 1.25)]
    idx = {"n": 0}

    def _get_totals() -> tuple[int, float]:
        value = totals[idx["n"]]
        idx["n"] = min(idx["n"] + 1, len(totals) - 1)
        return value

    with HeartbeatPinger(client, tick_id=7, get_totals=_get_totals, interval=0.05):
        _wait_for(lambda: len(client.calls) >= 3)

    assert len(client.calls) >= 3
    assert all(call[0] == 7 for call in client.calls)
    # First call used totals[0]; the advancing getter proves live totals are sent.
    assert client.calls[0][1:] == (0, 0.0)
    assert client.calls[-1][1:] != (0, 0.0)


def test_heartbeat_survives_api_errors() -> None:
    """One failed ping must not kill the pinger; the next interval must fire."""
    client = _FakeClient(fail_on={0, 1})
    with HeartbeatPinger(client, tick_id=9, get_totals=lambda: (0, 0.0), interval=0.05):
        _wait_for(lambda: len(client.calls) >= 4)
    assert len(client.calls) >= 4


def test_heartbeat_swallows_totals_getter_failure() -> None:
    client = _FakeClient()

    def _boom() -> tuple[int, float]:
        msg = "intentional"
        raise RuntimeError(msg)

    with HeartbeatPinger(client, tick_id=11, get_totals=_boom, interval=0.05):
        _wait_for(lambda: len(client.calls) >= 2)
    # The getter blew up, but the pinger degraded to (0, 0.0) instead of stopping.
    assert all(call == (11, 0, 0.0) for call in client.calls)


def test_heartbeat_stops_cleanly() -> None:
    client = _FakeClient()
    pinger = HeartbeatPinger(client, tick_id=1, get_totals=lambda: (0, 0.0), interval=0.05)
    pinger.start()
    _wait_for(lambda: len(client.calls) >= 1)
    pinger.stop()
    calls_after_stop = len(client.calls)
    time.sleep(0.2)
    # No additional pings after stop().
    assert len(client.calls) == calls_after_stop
