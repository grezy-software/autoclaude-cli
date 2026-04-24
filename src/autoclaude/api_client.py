"""HTTP client around the AutoClaude server API with self-healing docs protocol.

On failure, ``_attempt`` advances a per-``(endpoint, method)`` stage and enriches the
raised ``ApiError`` with a rendered docs payload pulled from either the local cache
(first failure), the server's sibling ``docs/`` route (second failure), or with a
persisted report + staff-visible POST (third failure and beyond).

The protocol is designed for an LLM caller (Claude) that drives the retry by
reformatting the payload between invocations; the CLI itself does not retry. See
``autoclaude/docs.py`` for the state machine primitives.
"""

from __future__ import annotations

import contextlib
import json as _json
from pathlib import Path
from typing import TYPE_CHECKING, Any, Self
from urllib.parse import urlparse

import httpx

from autoclaude.docs import (
    STAGE_FRESH,
    STAGE_LOCAL,
    STAGE_REMOTE,
    STAGE_REPORTED,
    DocFetchError,
    DocProvider,
    PersistentAttemptTracker,
    ReportWriter,
    next_stage,
)

if TYPE_CHECKING:  # pragma: no cover
    from autoclaude.config import Profile

REPORT_ENDPOINT = "/api/ac/runner/report/"


class ApiError(Exception):
    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        payload: Any = None,
        docs: str | None = None,
        docs_source: str = "none",
        stage: str = STAGE_FRESH,
        report_path: Path | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.payload = payload
        self.docs = docs
        self.docs_source = docs_source
        self.stage = stage
        self.report_path = report_path


class ApiClient:
    def __init__(
        self,
        profile: Profile,
        *,
        timeout: float = 30.0,
        autoclaude_root: Path | None = None,
        cli_version: str = "",
    ) -> None:
        if not profile.url:
            msg = f"url is empty for profile {profile.name!r}"
            raise ApiError(msg)
        self._client = httpx.Client(
            base_url=profile.url.rstrip("/"),
            timeout=timeout,
            headers={"Authorization": f"Api-Key {profile.api_key}"} if profile.api_key else {},
            follow_redirects=True,
        )
        self._profile = profile
        self._cli_version = cli_version
        root = autoclaude_root or profile.resolve_autoclaude_root()
        self._root = root
        self._docs = DocProvider(self._client, root)
        self._tracker = PersistentAttemptTracker(root)
        self._reports = ReportWriter(root)

    def close(self: Self) -> None:
        self._client.close()

    def __enter__(self: Self) -> Self:
        return self

    def __exit__(self: Self, *_exc: object) -> None:
        self.close()

    # --- public helpers -----------------------------------------------------

    @property
    def autoclaude_root(self: Self) -> Path:
        return self._root

    def tracker_snapshot(self: Self) -> dict[str, str]:
        return self._tracker.snapshot()

    # --- core protocol -------------------------------------------------------

    def _parse_body(self: Self, response: httpx.Response) -> Any:
        try:
            return response.json()
        except (ValueError, httpx.DecodingError):
            return response.text

    def _handle_failure(
        self: Self,
        response: httpx.Response,
        *,
        docs_path: str,
        method: str,
    ) -> ApiError:
        current = self._tracker.read(docs_path, method)
        upcoming = next_stage(current)
        body = self._parse_body(response)
        msg = f"{response.request.method} {response.request.url} -> {response.status_code}"
        docs_text: str | None = None
        docs_source = "none"
        report_path: Path | None = None

        if upcoming == STAGE_LOCAL:
            docs_text = self._docs.read_local(docs_path, method)
            docs_source = "local" if docs_text else "none"
        elif upcoming == STAGE_REMOTE:
            try:
                fetched = self._docs.fetch_remote(docs_path, method)
                docs_text = fetched.markdown
                docs_source = "remote"
            except (DocFetchError, httpx.HTTPError):
                docs_text = self._docs.read_local(docs_path, method)
                docs_source = "local" if docs_text else "none"
        elif upcoming == STAGE_REPORTED:
            report_path = self._escalate_report(
                docs_path=docs_path,
                method=method,
                status_code=response.status_code,
                request_body=_extract_request_body(response.request),
                response_body=body,
                prior_stages=[STAGE_LOCAL, STAGE_REMOTE, STAGE_REPORTED],
            )

        self._tracker.write(docs_path, method, upcoming)
        return ApiError(
            msg,
            status_code=response.status_code,
            payload=body,
            docs=docs_text,
            docs_source=docs_source,
            stage=upcoming,
            report_path=report_path,
        )

    def _escalate_report(
        self: Self,
        *,
        docs_path: str,
        method: str,
        status_code: int,
        request_body: Any,
        response_body: Any,
        prior_stages: list[str],
    ) -> Path:
        payload: dict[str, object] = {
            "endpoint": docs_path,
            "http_method": method.upper(),
            "status_code": status_code,
            "request_payload": _coerce_payload(request_body),
            "response_payload": _coerce_payload(response_body),
            "stages": prior_stages,
            "cli_version": self._cli_version,
        }
        report_path = self._reports.write(payload)
        with contextlib.suppress(httpx.HTTPError, OSError):
            self._client.post(REPORT_ENDPOINT, json=payload)
        return report_path

    def _attempt(
        self: Self,
        method: str,
        path: str,
        *,
        docs_path: str | None = None,
        json: dict | None = None,
    ) -> Any:
        effective_docs_path = docs_path or _strip_host(path)
        lookup_method = method.lower()
        request_kwargs: dict[str, Any] = {}
        if json is not None:
            request_kwargs["json"] = json
        response = self._client.request(method, path, **request_kwargs)
        if response.status_code < 400:
            self._tracker.reset(effective_docs_path, lookup_method)
            if not response.content:
                return None
            return response.json()
        raise self._handle_failure(
            response,
            docs_path=effective_docs_path,
            method=lookup_method,
        )

    # --- AutoClaude endpoints -------------------------------------------------

    def context(self: Self) -> dict[str, Any]:
        return self._attempt("GET", "/api/ac/runner/context/", docs_path="/api/ac/runner/context/")

    def open_tick(self: Self, *, runner_version: str, project_id: int | None = None) -> dict[str, Any]:
        body: dict[str, Any] = {"runner_version": runner_version}
        if project_id is not None:
            body["project_id"] = project_id
        return self._attempt("POST", "/api/ac/runner/tick/", docs_path="/api/ac/runner/tick/", json=body)

    def close_tick(
        self: Self,
        tick_id: int,
        *,
        status: str,
        outcome: str = "",
        error_log: str = "",
        cost_usd: float = 0.0,
        token_cost_estimate: int = 0,
    ) -> dict[str, Any]:
        return self._attempt(
            "PATCH",
            f"/api/ac/runner/{tick_id}/tick_close/",
            docs_path="/api/ac/runner/tick_close/",
            json={
                "status": status,
                "outcome": outcome,
                "error_log": error_log,
                "claude_cost_usd": float(cost_usd),
                "token_cost_estimate": int(token_cost_estimate),
            },
        )

    def open_step(
        self: Self,
        *,
        tick_id: int,
        agent_slug: str,
        ordinal: int,
        name: str,
    ) -> dict[str, Any]:
        return self._attempt(
            "POST",
            "/api/ac/runner/tick_step/",
            docs_path="/api/ac/runner/tick_step/",
            json={"tick_id": tick_id, "agent_slug": agent_slug, "ordinal": ordinal, "name": name},
        )

    def close_step(
        self: Self,
        step_id: int,
        *,
        summary: str = "",
        error_log: str = "",
        cost_usd: float = 0.0,
        token_cost_estimate: int = 0,
    ) -> dict[str, Any]:
        return self._attempt(
            "PATCH",
            f"/api/ac/runner/{step_id}/tick_step_close/",
            docs_path="/api/ac/runner/tick_step_close/",
            json={
                "summary": summary,
                "error_log": error_log,
                "claude_cost_usd": float(cost_usd),
                "token_cost_estimate": int(token_cost_estimate),
            },
        )

    def tick_heartbeat(
        self: Self,
        tick_id: int,
        *,
        token_cost_estimate: int | None = None,
        cost_usd: float | None = None,
    ) -> dict[str, Any]:
        """Ping the server so the reaper doesn't mark the tick abandoned.

        Optionally ships running totals so the UI can display live progress.
        """
        payload: dict[str, Any] = {}
        if token_cost_estimate is not None:
            payload["token_cost_estimate"] = int(token_cost_estimate)
        if cost_usd is not None:
            payload["claude_cost_usd"] = float(cost_usd)
        return self._attempt(
            "PATCH",
            f"/api/ac/runner/{tick_id}/tick_heartbeat/",
            docs_path="/api/ac/runner/tick_heartbeat/",
            json=payload,
        )

    def post_tick_logs(self: Self, tick_id: int, entries: list[dict[str, Any]]) -> dict[str, Any]:
        """Upload a batch of structured log entries for a tick."""
        return self._attempt(
            "POST",
            f"/api/ac/runner/{tick_id}/tick_log/",
            docs_path="/api/ac/runner/tick_log/",
            json={"entries": entries},
        )

    def debug_file_request_pending(self: Self) -> list[dict[str, Any]]:
        """List pending DebugFileRequests targeting ticks this runner owns."""
        result = self._attempt(
            "GET",
            "/api/ac/runner/debug_file_request_pending/",
            docs_path="/api/ac/runner/debug_file_request_pending/",
        )
        return list(result) if isinstance(result, list) else []

    def debug_file_request_fulfill(
        self: Self,
        request_id: int,
        *,
        content: str = "",
        content_truncated: bool = False,
        reason: str = "",
    ) -> dict[str, Any]:
        """Upload file contents (or a denial reason) for a specific DebugFileRequest."""
        return self._attempt(
            "PATCH",
            f"/api/ac/runner/{request_id}/debug_file_request_fulfill/",
            docs_path="/api/ac/runner/debug_file_request_fulfill/",
            json={
                "content": content,
                "content_truncated": content_truncated,
                "reason": reason,
            },
        )

    def get_tool_manifest(self: Self, slug: str) -> dict[str, Any]:
        """Fetch the full local-install manifest for ``slug``.

        Response shape: ``{"slug": str, "manifest_hash": str, "manifest": {...}}``.
        """
        return self._attempt(
            "GET",
            f"/api/ac/tools/manifest/?slug={slug}",
            docs_path="/api/ac/tools/manifest/",
        )

    def post_discord_message(self: Self, agent_config_id: int, content: str) -> dict[str, Any]:
        """Ask the server to POST ``content`` to the team's Discord webhook."""
        return self._attempt(
            "POST",
            "/api/ac/tools/discord/post/",
            docs_path="/api/ac/tools/discord/post/",
            json={"agent_config_id": agent_config_id, "content": content},
        )


def _strip_host(path_or_url: str) -> str:
    parsed = urlparse(path_or_url)
    return parsed.path if parsed.scheme else path_or_url


def _extract_request_body(request: httpx.Request) -> Any:
    body = request.content
    if not body:
        return None
    try:
        return _json.loads(body)
    except (ValueError, TypeError):
        try:
            return body.decode("utf-8", errors="replace")
        except (UnicodeDecodeError, AttributeError):
            return None


def _coerce_payload(value: Any) -> Any:
    if value is None:
        return {}
    if isinstance(value, (dict, list)):
        return value
    return {"raw": value}


__all__ = [
    "STAGE_FRESH",
    "STAGE_LOCAL",
    "STAGE_REMOTE",
    "STAGE_REPORTED",
    "ApiClient",
    "ApiError",
]
