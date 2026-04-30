"""Tests for the long-running heartbeat daemon."""

from __future__ import annotations

import threading
import time
from typing import Any

import pytest

from autoclaude.api_client import ApiError
from autoclaude.daemon import MIN_INTERVAL_SECONDS, Daemon
from autoclaude.installation import InstallationIdentity


class _FakeProfile:
    name = "test"


class _FakeClient:
    def __init__(self) -> None:
        self.heartbeat_calls: list[dict[str, Any]] = []
        self._lock = threading.Lock()
        self.profile = _FakeProfile()

    def heartbeat(self, **kwargs: Any) -> dict[str, Any]:
        with self._lock:
            self.heartbeat_calls.append(kwargs)
        return {"ok": True}


def _identity() -> InstallationIdentity:
    return InstallationIdentity(installation_id="test-uuid", hostname="laptop", os_platform="darwin")


def _wait_for(predicate, timeout: float = 2.0, poll: float = 0.01) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(poll)
    pytest.fail(f"predicate never held within {timeout}s")


def test_daemon_pings_on_interval() -> None:
    client = _FakeClient()
    daemon = Daemon(client, identity=_identity(), interval=5.0)
    daemon._interval = 0.05  # noqa: SLF001 (test override)

    thread = threading.Thread(target=daemon.run_forever)
    thread.start()
    try:
        _wait_for(lambda: len(client.heartbeat_calls) >= 3)
    finally:
        daemon.request_stop()
        thread.join(timeout=2.0)
    assert len(client.heartbeat_calls) >= 3
    assert all(call["installation_id"] == "test-uuid" for call in client.heartbeat_calls)


def test_daemon_survives_heartbeat_api_error() -> None:
    class _FlakyClient(_FakeClient):
        def __init__(self) -> None:
            super().__init__()
            self._n = 0

        def heartbeat(self, **kwargs: Any) -> dict[str, Any]:
            self._n += 1
            if self._n == 1:
                msg = "simulated transport blip"
                raise ApiError(msg)
            return super().heartbeat(**kwargs)

    client = _FlakyClient()
    daemon = Daemon(client, identity=_identity(), interval=5.0)
    daemon._interval = 0.05  # noqa: SLF001

    thread = threading.Thread(target=daemon.run_forever)
    thread.start()
    try:
        _wait_for(lambda: len(client.heartbeat_calls) >= 2)
    finally:
        daemon.request_stop()
        thread.join(timeout=2.0)
    assert len(client.heartbeat_calls) >= 2


def test_daemon_honors_server_dial_up() -> None:
    """If the heartbeat response carries `next_heartbeat_in_seconds`, the daemon adopts it (clamped)."""

    class _DialingClient(_FakeClient):
        def heartbeat(self, **kwargs: Any) -> dict[str, Any]:
            super().heartbeat(**kwargs)
            return {"ok": True, "next_heartbeat_in_seconds": 999, "tasks": []}

    client = _DialingClient()
    daemon = Daemon(client, identity=_identity(), interval=5.0)
    daemon._interval = 0.05  # noqa: SLF001

    thread = threading.Thread(target=daemon.run_forever)
    thread.start()
    try:
        _wait_for(lambda: len(client.heartbeat_calls) >= 1)
        _wait_for(lambda: daemon._interval >= MIN_INTERVAL_SECONDS)  # noqa: SLF001
    finally:
        daemon.request_stop()
        thread.join(timeout=2.0)


def test_daemon_stops_promptly_on_request() -> None:
    client = _FakeClient()
    daemon = Daemon(client, identity=_identity(), interval=5.0)
    daemon._interval = 0.05  # noqa: SLF001

    thread = threading.Thread(target=daemon.run_forever)
    thread.start()
    _wait_for(lambda: len(client.heartbeat_calls) >= 1)
    daemon.request_stop()
    thread.join(timeout=2.0)
    assert not thread.is_alive()
