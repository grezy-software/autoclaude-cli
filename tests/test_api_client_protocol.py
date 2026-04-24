"""End-to-end tests for the ApiClient 3-stage error-handling protocol."""

# Tests poke ApiClient internals (the _client transport, the _docs provider) to
# swap httpx transports. Ruff's "private member accessed" rule is deliberately
# suppressed for this file; production code does not reach across the boundary.
# ruff: noqa: SLF001

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from autoclaude.api_client import ApiClient, ApiError
from autoclaude.config import DEFAULT_URL, Profile
from autoclaude.docs import STAGE_FRESH, STAGE_LOCAL, STAGE_REMOTE, STAGE_REPORTED


def _profile(url: str = DEFAULT_URL) -> Profile:
    return Profile(name="test", url=url, api_key="k")


def _install_client(client: ApiClient, handler) -> None:
    transport = httpx.MockTransport(handler)
    client._client.close()
    client._client = httpx.Client(
        base_url=client._profile.url.rstrip("/"),
        timeout=30.0,
        headers={"Authorization": f"Api-Key {client._profile.api_key}"},
        transport=transport,
    )
    # Rewire the DocProvider to the same client so docs fetches use the mock.
    client._docs = type(client._docs)(client._client, client.autoclaude_root)  # type: ignore[misc]


def test_successful_call_keeps_tracker_fresh(tmp_path: Path) -> None:
    client = ApiClient(_profile(), autoclaude_root=tmp_path, cli_version="0.1.0")

    def handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": True})

    _install_client(client, handler)
    assert client.context() == {"ok": True}
    assert client.tracker_snapshot() == {}


def test_first_failure_attaches_local_doc_when_present(tmp_path: Path) -> None:
    client = ApiClient(_profile(), autoclaude_root=tmp_path, cli_version="0.1.0")
    # Pre-seed a local doc.
    local = client._docs.local_path("/api/ac/runner/context/", "get")
    local.parent.mkdir(parents=True, exist_ok=True)
    local.write_text("# cached docs for context", encoding="utf-8")

    def handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"detail": "bad"})

    _install_client(client, handler)
    with pytest.raises(ApiError) as exc:
        client.context()
    assert exc.value.stage == STAGE_LOCAL
    assert exc.value.docs_source == "local"
    assert exc.value.docs and "cached docs" in exc.value.docs


def test_first_failure_without_local_doc_reports_no_docs(tmp_path: Path) -> None:
    client = ApiClient(_profile(), autoclaude_root=tmp_path, cli_version="0.1.0")

    def handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"detail": "bad"})

    _install_client(client, handler)
    with pytest.raises(ApiError) as exc:
        client.context()
    assert exc.value.stage == STAGE_LOCAL
    assert exc.value.docs_source == "none"
    assert exc.value.docs is None


def test_three_consecutive_failures_walk_through_stages(tmp_path: Path) -> None:
    client = ApiClient(_profile(), autoclaude_root=tmp_path, cli_version="0.1.0")

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/docs/"):
            return httpx.Response(
                200,
                text="# fresh remote docs\n",
                headers={"ETag": '"etag-1"', "Content-Type": "text/markdown"},
            )
        if request.url.path == "/api/ac/runner/report/":
            return httpx.Response(201, json={"id": 1})
        return httpx.Response(400, json={"detail": "bad"})

    _install_client(client, handler)

    # First failure -> stage=local, no local doc present.
    with pytest.raises(ApiError) as first:
        client.context()
    assert first.value.stage == STAGE_LOCAL
    assert first.value.docs_source == "none"

    # Second failure -> stage=remote, doc fetched and cached.
    with pytest.raises(ApiError) as second:
        client.context()
    assert second.value.stage == STAGE_REMOTE
    assert second.value.docs_source == "remote"
    assert "fresh remote docs" in (second.value.docs or "")
    assert client._docs.read_local("/api/ac/runner/context/", "get") == "# fresh remote docs\n"

    # Third failure -> stage=reported, report JSON written.
    with pytest.raises(ApiError) as third:
        client.context()
    assert third.value.stage == STAGE_REPORTED
    assert third.value.report_path is not None and third.value.report_path.exists()
    report_data = json.loads(third.value.report_path.read_text(encoding="utf-8"))
    assert report_data["endpoint"] == "/api/ac/runner/context/"
    assert report_data["http_method"] == "GET"


def test_success_between_failures_resets_stage(tmp_path: Path) -> None:
    responses = iter(
        [
            httpx.Response(400, json={"detail": "first bad"}),  # first failure
            httpx.Response(200, json={"ok": True}),  # success resets
            httpx.Response(400, json={"detail": "second bad"}),  # starts at stage=local again
        ],
    )
    client = ApiClient(_profile(), autoclaude_root=tmp_path, cli_version="0.1.0")

    def handler(_req: httpx.Request) -> httpx.Response:
        return next(responses)

    _install_client(client, handler)
    with pytest.raises(ApiError):
        client.context()
    assert client.context() == {"ok": True}
    with pytest.raises(ApiError) as exc:
        client.context()
    assert exc.value.stage == STAGE_LOCAL


def test_report_network_failure_still_writes_local_file(tmp_path: Path) -> None:
    client = ApiClient(_profile(), autoclaude_root=tmp_path, cli_version="0.1.0")

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/docs/"):
            return httpx.Response(200, text="# remote\n", headers={"ETag": '"e1"'})
        if request.url.path == "/api/ac/runner/report/":
            raise httpx.ConnectError("network down")
        return httpx.Response(400, json={"detail": "bad"})

    _install_client(client, handler)
    for _ in range(2):
        with pytest.raises(ApiError):
            client.context()
    with pytest.raises(ApiError) as third:
        client.context()
    assert third.value.stage == STAGE_REPORTED
    assert third.value.report_path is not None
    assert third.value.report_path.exists()


def test_stage_persists_across_client_instances(tmp_path: Path) -> None:
    c1 = ApiClient(_profile(), autoclaude_root=tmp_path, cli_version="0.1.0")

    def handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"detail": "bad"})

    _install_client(c1, handler)
    with pytest.raises(ApiError):
        c1.context()
    c1.close()

    c2 = ApiClient(_profile(), autoclaude_root=tmp_path, cli_version="0.1.0")
    _install_client(c2, handler)
    with pytest.raises(ApiError) as exc:
        c2.context()
    assert exc.value.stage == STAGE_REMOTE


def test_tracker_snapshot_reports_last_stage(tmp_path: Path) -> None:
    client = ApiClient(_profile(), autoclaude_root=tmp_path, cli_version="0.1.0")

    def handler(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"detail": "bad"})

    _install_client(client, handler)
    with pytest.raises(ApiError):
        client.context()
    snapshot = client.tracker_snapshot()
    assert snapshot.get("ac_runner_context:get") == STAGE_LOCAL


def test_stage_fresh_after_fresh_install() -> None:
    assert STAGE_FRESH == "fresh"
