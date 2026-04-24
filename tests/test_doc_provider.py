"""Tests for the DocProvider, AttemptTracker, and ReportWriter primitives."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from autoclaude.docs import (
    STAGE_FRESH,
    STAGE_LOCAL,
    STAGE_REMOTE,
    STAGE_REPORTED,
    DocFetchError,
    DocProvider,
    PersistentAttemptTracker,
    ReportWriter,
    docs_url,
    endpoint_slug,
    next_stage,
)

# --- slug + url helpers -----------------------------------------------------


def test_endpoint_slug_strips_api_prefix_and_replaces_slashes() -> None:
    assert endpoint_slug("/api/ac/runner/context/") == "ac_runner_context"
    assert endpoint_slug("/api/ac/runner/tick_close/") == "ac_runner_tick_close"
    assert endpoint_slug("ac/runner/report") == "ac_runner_report"


def test_docs_url_appends_docs_suffix() -> None:
    assert docs_url("/api/ac/runner/context/") == "/api/ac/runner/context/docs/"
    assert docs_url("/api/ac/runner/tick") == "/api/ac/runner/tick/docs/"


def test_next_stage_advances_then_saturates() -> None:
    assert next_stage(STAGE_FRESH) == STAGE_LOCAL
    assert next_stage(STAGE_LOCAL) == STAGE_REMOTE
    assert next_stage(STAGE_REMOTE) == STAGE_REPORTED
    assert next_stage(STAGE_REPORTED) == STAGE_REPORTED


# --- DocProvider ------------------------------------------------------------


def _build_http(handler):
    transport = httpx.MockTransport(handler)
    return httpx.Client(base_url="http://test", transport=transport)


def test_doc_provider_local_miss_returns_none(tmp_path: Path) -> None:
    http = _build_http(lambda _req: httpx.Response(500))
    provider = DocProvider(http, tmp_path)
    assert provider.read_local("/api/ac/runner/context/", "get") is None


def test_doc_provider_fetches_remote_writes_cache_and_etag(tmp_path: Path) -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(
            200,
            text="# GET /api/ac/runner/context/\n",
            headers={"ETag": '"abc123"', "Content-Type": "text/markdown"},
        )

    http = _build_http(handler)
    provider = DocProvider(http, tmp_path)
    result = provider.fetch_remote("/api/ac/runner/context/", "get")
    assert result.markdown.startswith("# GET")
    assert result.etag == "abc123"
    cached = provider.read_local("/api/ac/runner/context/", "get")
    assert cached == result.markdown
    assert provider.read_local_etag("/api/ac/runner/context/", "get") == "abc123"
    assert len(calls) == 1


def test_doc_provider_sends_if_none_match_and_honors_304(tmp_path: Path) -> None:
    calls: list[httpx.Request] = []
    responses = iter(
        [
            httpx.Response(200, text="# docs v1\n", headers={"ETag": '"v1"'}),
            httpx.Response(304, headers={"ETag": '"v1"'}),
        ],
    )

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return next(responses)

    http = _build_http(handler)
    provider = DocProvider(http, tmp_path)
    first = provider.fetch_remote("/api/ac/runner/tick/", "post")
    assert first.etag == "v1"

    second = provider.fetch_remote("/api/ac/runner/tick/", "post")
    assert second.markdown == first.markdown
    assert calls[1].headers.get("If-None-Match") == '"v1"'


def test_doc_provider_raises_on_http_error(tmp_path: Path) -> None:
    http = _build_http(lambda _req: httpx.Response(500, text="boom"))
    provider = DocProvider(http, tmp_path)
    with pytest.raises(DocFetchError) as exc:
        provider.fetch_remote("/api/ac/runner/tick/", "post")
    assert exc.value.status_code == 500


# --- PersistentAttemptTracker ----------------------------------------------


def test_tracker_returns_fresh_by_default(tmp_path: Path) -> None:
    tracker = PersistentAttemptTracker(tmp_path)
    assert tracker.read("/api/ac/runner/context/", "get") == STAGE_FRESH


def test_tracker_persists_across_instances(tmp_path: Path) -> None:
    t1 = PersistentAttemptTracker(tmp_path)
    t1.write("/api/ac/runner/tick/", "post", STAGE_LOCAL)
    t2 = PersistentAttemptTracker(tmp_path)
    assert t2.read("/api/ac/runner/tick/", "post") == STAGE_LOCAL


def test_tracker_resets_after_reset_call(tmp_path: Path) -> None:
    tracker = PersistentAttemptTracker(tmp_path)
    tracker.write("/api/ac/runner/tick/", "post", STAGE_REMOTE)
    tracker.reset("/api/ac/runner/tick/", "post")
    assert tracker.read("/api/ac/runner/tick/", "post") == STAGE_FRESH


def test_tracker_expires_stale_entries(tmp_path: Path) -> None:
    tracker = PersistentAttemptTracker(tmp_path)
    tracker.write("/api/ac/runner/tick/", "post", STAGE_REMOTE)
    # Rewrite state file with a very old timestamp.
    state_path = tmp_path / "state" / "attempts.json"
    raw = json.loads(state_path.read_text(encoding="utf-8"))
    for entry in raw.values():
        entry["updated_at"] = 0  # 1970
    state_path.write_text(json.dumps(raw), encoding="utf-8")
    assert tracker.read("/api/ac/runner/tick/", "post") == STAGE_FRESH


# --- ReportWriter -----------------------------------------------------------


def test_report_writer_creates_json_file(tmp_path: Path) -> None:
    writer = ReportWriter(tmp_path)
    path = writer.write(
        {
            "endpoint": "/api/ac/runner/tick/",
            "http_method": "POST",
            "status_code": 400,
            "request_payload": {"runner_version": "0.1.0"},
            "response_payload": {"detail": "bad"},
            "stages": [STAGE_LOCAL, STAGE_REMOTE, STAGE_REPORTED],
            "cli_version": "0.1.0",
        },
    )
    assert path.exists()
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["status_code"] == 400
    assert data["http_method"] == "POST"
    assert writer.count() == 1
