"""Tests for the TickLogger context manager."""

from __future__ import annotations

import json
import logging
from typing import Any

import httpx
import pytest

from autoclaude.api_client import ApiClient
from autoclaude.config import Profile
from autoclaude.logger import get_logger
from autoclaude.tick_logger import TickLogger


@pytest.fixture
def api(tmp_path, monkeypatch) -> ApiClient:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    profile = Profile(name="t", url="http://localhost:9", api_key="k")
    return ApiClient(profile)


def test_system_context_emitted_on_enter(api, httpx_mock, tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    captured: list[dict[str, Any]] = []

    def _ok(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        captured.extend(body["entries"])
        return httpx.Response(202, json={"accepted": len(body["entries"]), "submitted": len(body["entries"])})

    httpx_mock.add_callback(_ok, url="http://localhost:9/api/ac/runner/5/tick_log/", is_reusable=True)

    with TickLogger(api, tick_id=5):
        pass

    sources = [e["source"] for e in captured]
    assert "system" in sources


def test_unhandled_exception_flushes_traceback(api, httpx_mock, tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    captured: list[dict[str, Any]] = []

    def _ok(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        captured.extend(body["entries"])
        return httpx.Response(202, json={"accepted": len(body["entries"]), "submitted": len(body["entries"])})

    httpx_mock.add_callback(_ok, url="http://localhost:9/api/ac/runner/6/tick_log/", is_reusable=True)

    with pytest.raises(RuntimeError), TickLogger(api, tick_id=6):
        log = get_logger("tick")
        log.info("before crash", extra={"source": "cli"})
        raise RuntimeError("boom")

    sources = [e["source"] for e in captured]
    assert "traceback" in sources
    traceback_entry = next(e for e in captured if e["source"] == "traceback")
    assert "RuntimeError" in traceback_entry["message"]
    assert "boom" in traceback_entry["payload"]["traceback"]


def test_client_seq_is_monotonic_across_records(api, httpx_mock, tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    captured: list[dict[str, Any]] = []

    def _ok(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        captured.extend(body["entries"])
        return httpx.Response(202, json={"accepted": len(body["entries"]), "submitted": len(body["entries"])})

    httpx_mock.add_callback(_ok, url="http://localhost:9/api/ac/runner/7/tick_log/", is_reusable=True)

    log = get_logger("tick")
    with TickLogger(api, tick_id=7):
        for i in range(3):
            log.info("entry-%d", i, extra={"source": "cli"})

    # System context plus 3 user entries = at least 4 records; seqs strictly increasing.
    seqs = [e["client_seq"] for e in captured]
    assert seqs == sorted(seqs)
    assert len(set(seqs)) == len(seqs)


def test_handler_is_detached_on_exit(api, httpx_mock, tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    httpx_mock.add_response(
        url="http://localhost:9/api/ac/runner/8/tick_log/",
        method="POST",
        json={"accepted": 0, "submitted": 0},
        status_code=202,
        is_reusable=True,
        is_optional=True,
    )

    root = logging.getLogger("autoclaude")
    before = list(root.handlers)
    with TickLogger(api, tick_id=8):
        assert len(root.handlers) == len(before) + 1
    assert root.handlers == before
