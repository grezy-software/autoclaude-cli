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
from datetime import datetime
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

    @property
    def base_url(self: Self) -> str:
        """Server URL (no trailing slash). Used to build env vars for sub-tools."""
        return str(self._client.base_url).rstrip("/")

    @property
    def api_key(self: Self) -> str:
        """Raw API key for the active profile. Used to build env vars for sub-tools."""
        return self._profile.api_key or ""

    @property
    def profile(self: Self) -> Profile:
        """Profile this client speaks for. Used by multi-profile loops to log per-profile context."""
        return self._profile

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
        try:
            response = self._client.request(method, path, **request_kwargs)
        except httpx.RequestError as exc:
            # Network/timeout/DNS issues never reach _handle_failure (no
            # response object). Surface them as ApiError so callers like the
            # daemon's _tick_once can swallow them and retry on the next loop
            # instead of crashing the process and forcing a launchd restart.
            msg = f"{method} {self._client.base_url}{path} -> {type(exc).__name__}: {exc}"
            raise ApiError(msg) from exc
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
        agent_slug: str = "",
        ordinal: int,
        name: str,
        kind: str = "agent",
        action: str = "",
        started_at: datetime | None = None,
    ) -> dict[str, Any]:
        """Open a TickStep.

        ``kind`` is ``"setup"``/``"agent"``/``"cleanup"``. ``action`` is the prompt
        (for agents) or a short command description (for setup/cleanup) surfaced
        in the dashboard. ``started_at`` is honoured when supplied so the CLI can
        back-date rows for work that ran before the Tick existed.
        """
        payload: dict[str, Any] = {
            "tick_id": tick_id,
            "kind": kind,
            "ordinal": ordinal,
            "name": name,
        }
        if agent_slug:
            payload["agent_slug"] = agent_slug
        if action:
            payload["action"] = action
        if started_at is not None:
            payload["started_at"] = started_at.isoformat()
        return self._attempt(
            "POST",
            "/api/ac/runner/tick_step/",
            docs_path="/api/ac/runner/tick_step/",
            json=payload,
        )

    def close_step(
        self: Self,
        step_id: int,
        *,
        summary: str = "",
        error_log: str = "",
        cost_usd: float = 0.0,
        token_cost_estimate: int = 0,
        ended_at: datetime | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "summary": summary,
            "error_log": error_log,
            "claude_cost_usd": float(cost_usd),
            "token_cost_estimate": int(token_cost_estimate),
        }
        if ended_at is not None:
            payload["ended_at"] = ended_at.isoformat()
        return self._attempt(
            "PATCH",
            f"/api/ac/runner/{step_id}/tick_step_close/",
            docs_path="/api/ac/runner/tick_step_close/",
            json=payload,
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

    def upload_tick_file_tree(self: Self, tick_id: int, snapshot: dict[str, Any]) -> dict[str, Any]:
        """Upload a capped ``.autoclaude/`` layout snapshot at tick close.

        The dashboard renders the tree in the DebugFileRequest UI so operators
        can browse what files are available instead of guessing paths.
        """
        return self._attempt(
            "PATCH",
            f"/api/ac/runner/{tick_id}/tick_file_tree/",
            docs_path="/api/ac/runner/tick_file_tree/",
            json=snapshot,
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

    def heartbeat(
        self: Self,
        *,
        installation_id: str,
        hostname: str = "",
        os_platform: str = "",
        cli_version: str = "",
        team_id: int | None = None,
        claude_usage: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Tell the server this CLI install is alive and pull any pending small tasks.

        Independent of the per-tick heartbeat. Returns a payload of the
        shape ``{ok, next_heartbeat_in_seconds, tasks: [...]}``.

        ``claude_usage`` (optional) is a rate_limits sample captured by the
        Claude Code status line; the daemon ships it at most every 15 minutes
        so the dashboard can chart subscription usage over time.
        """
        payload: dict[str, Any] = {
            "installation_id": installation_id,
            "hostname": hostname,
            "os_platform": os_platform,
            "cli_version": cli_version or self._cli_version,
        }
        if team_id is not None:
            payload["team_id"] = int(team_id)
        if claude_usage is not None:
            payload["claude_usage"] = claude_usage
        return self._attempt(
            "POST",
            "/api/ac/runner/heartbeat/",
            docs_path="/api/ac/runner/heartbeat/",
            json=payload,
        )

    def runner_task_complete(
        self: Self,
        task_id: int,
        *,
        status: str,
        result: dict[str, Any] | None = None,
        error_log: str = "",
    ) -> dict[str, Any]:
        """Report completion (or failure) of a previously claimed RunnerTask."""
        return self._attempt(
            "PATCH",
            f"/api/ac/runner/{task_id}/runner_task_complete/",
            docs_path="/api/ac/runner/runner_task_complete/",
            json={
                "status": status,
                "result": result or {},
                "error_log": error_log,
            },
        )

    def create_task(
        self: Self,
        *,
        team_id: int,
        kind: str,
        title: str,
        body: str = "",
        action_url: str = "",
        payload: dict[str, Any] | None = None,
        source: str = "",
        dedupe_key: str = "",
        project_id: int | None = None,
        tick_id: int | None = None,
        user_id: int | None = None,
        is_blocking: bool = False,
    ) -> dict[str, Any]:
        """Create (or refresh) a user-actionable Task on the server.

        Posts to ``/api/ac/task/``. When ``dedupe_key`` is set and a non-terminal
        task already exists for ``(user, dedupe_key)``, the server refreshes its
        title, body, action_url, and payload instead of creating a duplicate.
        """
        json_body: dict[str, Any] = {
            "team_id": int(team_id),
            "kind": kind,
            "title": title,
        }
        if body:
            json_body["body"] = body
        if action_url:
            json_body["action_url"] = action_url
        if payload:
            json_body["payload"] = payload
        if source:
            json_body["source"] = source
        if dedupe_key:
            json_body["dedupe_key"] = dedupe_key
        if project_id is not None:
            json_body["project_id"] = int(project_id)
        if tick_id is not None:
            json_body["tick_id"] = int(tick_id)
        if user_id is not None:
            json_body["user_id"] = int(user_id)
        if is_blocking:
            json_body["is_blocking"] = True
        return self._attempt(
            "POST",
            "/api/ac/task/",
            docs_path="/api/ac/task/",
            json=json_body,
        )

    def update_project_github_repo(self: Self, project_id: int, github_repo: str) -> dict[str, Any]:
        """Patch ``Project.github_repo`` after the CLI auto-creates the repo on GitHub.

        Used by the runner's auto-create flow: the first tick on a project
        with no ``github_repo`` set creates the repo via ``gh`` and then
        calls this so subsequent ticks resolve the same value from
        ``client.context()``.
        """
        return self._attempt(
            "PATCH",
            f"/api/ac/runner/{project_id}/project_github_repo/",
            docs_path="/api/ac/runner/project_github_repo/",
            json={"github_repo": github_repo},
        )


def _strip_host(path_or_url: str) -> str:
    parsed = urlparse(path_or_url)
    return parsed.path if parsed.scheme else path_or_url


def _extract_request_body(request: httpx.Request) -> Any:
    try:
        body = request.content
    except httpx.RequestNotRead:
        try:
            body = request.read()
        except Exception:  # noqa: BLE001
            return None
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
