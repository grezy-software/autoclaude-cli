"""Tests for the queue-backed backend log uploader."""

from __future__ import annotations

import json
import logging
import time
from typing import Any

import httpx
import pytest

from autoclaude.api_client import ApiClient
from autoclaude.config import Profile
from autoclaude.log_uploader import (
    BackendLogHandler,
    BackendLogUploader,
    pending_path,
    replay_pending,
)


def _make_record(message: str = "hello", level: int = logging.INFO, extra: dict[str, Any] | None = None) -> logging.LogRecord:
    record = logging.LogRecord(
        name="autoclaude.test",
        level=level,
        pathname=__file__,
        lineno=1,
        msg=message,
        args=None,
        exc_info=None,
    )
    if extra:
        for k, v in extra.items():
            setattr(record, k, v)
    return record


@pytest.fixture
def api(tmp_path, monkeypatch) -> ApiClient:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    profile = Profile(name="t", url="http://localhost:9", api_key="k")
    return ApiClient(profile)


def test_seq_is_monotonic(api, httpx_mock, tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    captured: list[list[dict[str, Any]]] = []

    def _ok(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        captured.append(body["entries"])
        return httpx.Response(202, json={"accepted": len(body["entries"]), "submitted": len(body["entries"])})

    httpx_mock.add_callback(_ok, url="http://localhost:9/api/ac/runner/7/tick_log/", is_reusable=True)

    uploader = BackendLogUploader(api, tick_id=7, batch_size=2, flush_interval=0.1)
    for i in range(5):
        uploader.enqueue(_make_record(f"line-{i}"))
    assert uploader.flush(timeout=3.0)
    uploader.close(timeout=3.0)

    all_entries = [e for batch in captured for e in batch]
    seqs = [e["client_seq"] for e in all_entries]
    assert seqs == sorted(seqs)
    assert seqs == list(range(1, 6))


def test_spill_on_failure_then_replay(api, httpx_mock, tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    attempts: list[int] = []

    def _fail_then_succeed(_request: httpx.Request) -> httpx.Response:
        attempts.append(1)
        if len(attempts) == 1:
            return httpx.Response(500, json={"error": "boom"})
        return httpx.Response(202, json={"accepted": 1, "submitted": 1})

    httpx_mock.add_callback(_fail_then_succeed, url="http://localhost:9/api/ac/runner/42/tick_log/", is_reusable=True)

    uploader = BackendLogUploader(api, tick_id=42, batch_size=1, flush_interval=0.1)
    uploader.enqueue(_make_record("first"))
    # Wait for the first POST to fail and spill
    for _ in range(30):
        if pending_path(42).exists():
            break
        time.sleep(0.05)
    assert pending_path(42).exists(), "expected spill file to be created on failure"

    # Enqueue a second record; this one succeeds and triggers sidecar replay.
    uploader.enqueue(_make_record("second"))
    assert uploader.flush(timeout=3.0)
    uploader.close(timeout=3.0)
    assert not pending_path(42).exists(), "sidecar should be removed after successful replay"


def test_replay_pending_on_startup(api, httpx_mock, tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    logs_dir = tmp_path / "autoclaude" / "logs"
    logs_dir.mkdir(parents=True)
    file_path = logs_dir / "pending-99.ndjson"
    file_path.write_text(
        "\n".join(
            json.dumps(
                {
                    "client_seq": i,
                    "level": "info",
                    "source": "cli",
                    "message": f"orphaned-{i}",
                    "payload": {},
                    "client_ts": "2025-01-01T00:00:00Z",
                    "step_id": None,
                },
            )
            for i in range(1, 4)
        ),
        encoding="utf-8",
    )

    httpx_mock.add_response(
        url="http://localhost:9/api/ac/runner/99/tick_log/",
        method="POST",
        json={"accepted": 3, "submitted": 3},
        status_code=202,
    )

    replayed = replay_pending(api)
    assert replayed == 1
    assert not file_path.exists()


def test_replay_pending_drops_sidecar_on_4xx(api, httpx_mock, tmp_path, monkeypatch) -> None:
    """A stale sidecar that the server rejects with 4xx must be deleted, not retried forever."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    logs_dir = tmp_path / "autoclaude" / "logs"
    logs_dir.mkdir(parents=True)
    file_path = logs_dir / "pending-77.ndjson"
    file_path.write_text(
        json.dumps(
            {
                "client_seq": 1,
                "level": "info",
                "source": "cli",
                "message": "stale",
                "payload": {},
                "client_ts": "2025-01-01T00:00:00Z",
                "step_id": None,
            },
        )
        + "\n",
        encoding="utf-8",
    )

    # Server says the tick is gone (404). Replay must drop the file.
    httpx_mock.add_response(
        url="http://localhost:9/api/ac/runner/77/tick_log/",
        method="POST",
        status_code=404,
        json={"detail": "Tick not found."},
    )
    # The doc-protocol may follow up with a docs fetch; serve an empty response.
    httpx_mock.add_response(
        url="http://localhost:9/docs/api/ac/runner/tick_log/",
        method="GET",
        status_code=404,
        is_optional=True,
    )

    replayed = replay_pending(api)
    assert replayed == 0  # 4xx is not counted as a replay
    assert not file_path.exists(), "sidecar should be dropped on irrecoverable 4xx"


def test_handler_attaches_as_logging_handler(api, httpx_mock, tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    captured: list[dict[str, Any]] = []

    def _ok(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        captured.extend(body["entries"])
        return httpx.Response(202, json={"accepted": len(body["entries"]), "submitted": len(body["entries"])})

    httpx_mock.add_callback(_ok, url="http://localhost:9/api/ac/runner/1/tick_log/", is_reusable=True)

    uploader = BackendLogUploader(api, tick_id=1, flush_interval=0.1)
    handler = BackendLogHandler(uploader)
    logger = logging.getLogger("autoclaude.test.handler")
    logger.handlers.clear()
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    logger.info("from-logger", extra={"source": "cli", "payload": {"k": "v"}})
    assert uploader.flush(timeout=3.0)
    uploader.close(timeout=3.0)
    logger.removeHandler(handler)

    assert captured
    entry = captured[0]
    assert entry["source"] == "cli"
    assert entry["message"] == "from-logger"
    assert entry["payload"] == {"k": "v"}
