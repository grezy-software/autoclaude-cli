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
    def __init__(self, *, tasks_per_call: list[list[dict[str, Any]]] | None = None) -> None:
        self.heartbeat_calls: list[dict[str, Any]] = []
        self.completed: list[tuple[int, str, dict[str, Any], str]] = []
        self.fulfilled: list[dict[str, Any]] = []
        self._tasks = tasks_per_call or []
        self._lock = threading.Lock()
        self.profile = _FakeProfile()

    def heartbeat(self, **kwargs: Any) -> dict[str, Any]:
        with self._lock:
            self.heartbeat_calls.append(kwargs)
            tasks = self._tasks.pop(0) if self._tasks else []
        # Tests intentionally omit `next_heartbeat_in_seconds` so the daemon
        # keeps the (overridden) tight interval the test set up; we test the
        # dial-up path separately below.
        return {"ok": True, "tasks": tasks}

    def runner_task_complete(
        self,
        task_id: int,
        *,
        status: str,
        result: dict[str, Any] | None = None,
        error_log: str = "",
    ) -> dict[str, Any]:
        with self._lock:
            self.completed.append((task_id, status, result or {}, error_log))
        return {"ok": True}

    def debug_file_request_fulfill(
        self,
        request_id: int,
        *,
        content: str = "",
        content_truncated: bool = False,
        reason: str = "",
    ) -> dict[str, Any]:
        with self._lock:
            self.fulfilled.append(
                {
                    "request_id": request_id,
                    "content": content,
                    "truncated": content_truncated,
                    "reason": reason,
                },
            )
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


def test_daemon_dispatches_unknown_task_as_failed() -> None:
    client = _FakeClient(tasks_per_call=[[{"id": 1, "task_type": "no_such_handler", "payload": {}}]])
    daemon = Daemon(client, identity=_identity(), interval=5.0)
    daemon._interval = 0.05  # noqa: SLF001

    thread = threading.Thread(target=daemon.run_forever)
    thread.start()
    try:
        _wait_for(lambda: client.completed)
    finally:
        daemon.request_stop()
        thread.join(timeout=2.0)
    assert client.completed[0][0] == 1
    assert client.completed[0][1] == "failed"
    assert "unknown task_type" in client.completed[0][3]


def test_daemon_handles_debug_file_fulfill_without_local_context() -> None:
    payload = {"debug_file_request_id": 11, "tick_id": 99, "relative_path": "state/last_tick.json"}
    client = _FakeClient(tasks_per_call=[[{"id": 7, "task_type": "debug_file_fulfill", "payload": payload}]])
    daemon = Daemon(client, identity=_identity(), interval=5.0)
    daemon._interval = 0.05  # noqa: SLF001

    thread = threading.Thread(target=daemon.run_forever)
    thread.start()
    try:
        _wait_for(lambda: client.completed)
    finally:
        daemon.request_stop()
        thread.join(timeout=2.0)
    # No worktree exists for tick 99 -> handler denies via reason and reports fulfilled to the daemon.
    assert client.fulfilled[0]["reason"] == "daemon_no_local_context"
    assert client.completed[0][1] == "fulfilled"
    assert client.completed[0][2]["denied_reason"] == "daemon_no_local_context"


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
