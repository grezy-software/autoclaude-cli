"""Orchestrate one tick: fetch context, execute steps, close.

Every phase of a tick is recorded on the server as a ``TickStep`` row
with a ``kind`` label (setup / agent / cleanup) so the dashboard can
render the full lifecycle rather than agent work alone. Pre-tick-open
phases (repo sync, storage prep, tool reconcile) can't open a row live
because the ``Tick`` doesn't exist yet; we capture their wall-clock
timing + summary in ``_PendingLifecycleStep`` and flush them as
back-dated rows right after ``tick_open`` succeeds.
"""

from __future__ import annotations

import re
import signal
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from autoclaude import __version__
from autoclaude import gh as gh_helpers
from autoclaude import repo_config as repo_config_mod
from autoclaude.api_client import ApiClient, ApiError
from autoclaude.claude_proc import ClaudeResult, run_step
from autoclaude.file_tree import build_snapshot as build_file_tree_snapshot
from autoclaude.gh import GhError
from autoclaude.gh import ensure_installed as ensure_gh_installed
from autoclaude.heartbeat import HeartbeatPinger
from autoclaude.logger import get_logger
from autoclaude.storage import RepoStorage
from autoclaude.tick_logger import TickLogger
from autoclaude.tools.applier import apply_manifest
from autoclaude.tools.manifest import Manifest, ManifestRef
from autoclaude.workspace import Workspace, WorkspaceError, Worktree

if TYPE_CHECKING:
    from collections.abc import Callable

_log = get_logger("runner")

_ERROR_CHARS = 2000
_RESUMPTION_SUMMARY_MAX = 500
_PROMPT_ACTION_CHARS = 8_000
_LOG_FLUSH_TIMEOUT = 5.0

STATUS_SUCCEEDED = "succeeded"
STATUS_FAILED = "failed"
STATUS_TOKEN_EXHAUSTED = "token_exhausted"  # noqa: S105 (status label, not a secret)
STATUS_ABANDONED = "abandoned"

KIND_SETUP = "setup"
KIND_AGENT = "agent"
KIND_TOOL = "tool"
KIND_CLEANUP = "cleanup"

# Cap stdout passed into a tool dispatch prompt; very long agent runs would
# otherwise blow past Claude's context budget for the (small) tool step.
_TOOL_STDOUT_MAX_CHARS = 32_000

STEP_REPO_SYNC = "repo_sync"
STEP_STORAGE_PREP = "storage_prep"
STEP_TOOL_RECONCILE = "tool_reconcile"
STEP_WORKSPACE_PREP = "workspace_prep"
STEP_BRANCH_PUSH = "branch_push"
STEP_PR_OPEN = "pr_open"
STEP_FINALIZE = "finalize"
STEP_WORKSPACE_CLEANUP = "workspace_cleanup"
STEP_LOG_FLUSH = "log_flush"

# Exit codes consumed by cli.py so `autoclaude tick` callers can distinguish
# billing failures from generic failures from graceful shutdowns.
EXIT_OK = 0
EXIT_FAILED = 1
EXIT_TOKEN_EXHAUSTED = 3
EXIT_ABANDONED = 130
# Server returned 409 on tick_open: the user already holds a live tick, or
# the picked Job is mid-tick on another runner. Concurrency is enforced
# server-side; the CLI just exits cleanly so cron / launchd retries land on
# the next eligible window.
EXIT_LOCKED = 4


@dataclass
class _PendingLifecycleStep:
    """A setup phase that ran before ``tick_open`` could create a TickStep.

    The runner buffers these and flushes them as back-dated rows once a
    tick id is available.
    """

    name: str
    kind: str
    started_at: datetime
    ended_at: datetime
    action: str
    summary: str
    error_log: str = ""


@dataclass
class _TickState:
    tick_id: int
    total_cost: float = 0.0
    total_tokens: int = 0
    status: str = STATUS_SUCCEEDED
    error: str = ""
    outcomes: list[str] = field(default_factory=list)
    # Set after the branch_push cleanup step succeeds; surfaced in the tick
    # outcome so operators can click through to the changes.
    branch_url: str = ""
    pr_url: str = ""
    # First non-null ``agent_config_id`` seen across the plan's steps; used to
    # route tick-level notifications (Discord) to the right team webhook.
    agent_config_id: int | None = None


def _utcnow() -> datetime:
    return datetime.now(tz=UTC)


def _iso_now() -> str:
    return _utcnow().isoformat().replace("+00:00", "Z")


def _build_resumption_banner(resumed_from: dict[str, Any]) -> str:
    prior_id = resumed_from.get("tick_id")
    last_step = resumed_from.get("last_step") or {}
    agent = last_step.get("agent_slug") or "unknown"
    ordinal = last_step.get("ordinal")
    summary = (last_step.get("summary") or "").strip()[:_RESUMPTION_SUMMARY_MAX]
    header = f"[Resuming abandoned tick #{prior_id}."
    if ordinal is not None:
        header += f" Last completed step: {agent} (ordinal {ordinal})."
    else:
        header += " No prior step completed."
    if summary:
        return f"{header}\nSummary excerpt: {summary}\nContinue from where that stopped.]"
    return f"{header} Continue from where that stopped.]"


def _apply_resumption(steps: list[dict[str, Any]], resumed_from: dict[str, Any] | None) -> None:
    """Prepend a resumption banner to the first step's prompt, in-place."""
    if not resumed_from or not steps:
        return
    banner = _build_resumption_banner(resumed_from)
    first = steps[0]
    first["prompt"] = f"{banner}\n\n{first['prompt']}"


def _step_env(client: ApiClient, step: dict[str, Any], *, tick_id: int) -> dict[str, str]:
    """Env vars handed to the claude subprocess for tool slash commands.

    Tool manifests ship slash commands that curl back to the AutoClaude server
    (see ``apps.autoclaude.tools.discord``). Those commands read these env
    vars at execution time, so the runner must set them per step. Empty
    values are still passed through; the slash command surfaces the failure
    rather than the runner pre-judging it.
    """
    agent_config_id = step.get("agent_config_id")
    return {
        "AUTOCLAUDE_SERVER": client.base_url,
        "AUTOCLAUDE_API_KEY": client.api_key,
        "AUTOCLAUDE_AGENT_CONFIG_ID": "" if agent_config_id is None else str(agent_config_id),
        "AUTOCLAUDE_TICK_ID": str(tick_id),
    }


_DISCORD_ERROR_CHARS = 1500


def _notify_tick_failure(client: ApiClient, state: _TickState) -> None:
    """Post a tick-failure summary to the team's Discord webhook.

    No-ops when the tick succeeded, was abandoned by the user, or when no
    ``agent_config_id`` is available to route the message. Best-effort:
    Discord posting must never bubble up and break the tick close path.
    """
    if state.status != STATUS_FAILED:
        return
    if state.agent_config_id is None:
        _log.debug("skipping Discord failure notification: no agent_config_id on tick", extra={"source": "cli"})
        return
    error_excerpt = (state.error or "no error message").strip()[-_DISCORD_ERROR_CHARS:]
    content_lines = [
        f":x: AutoClaude tick #{state.tick_id} **FAILED**",
        f"**Error:** ```{error_excerpt}```",
        f"**Cost:** ${state.total_cost:.4f} | **Tokens:** {state.total_tokens}",
    ]
    if state.branch_url:
        content_lines.append(f"**Branch:** {state.branch_url}")
    if state.pr_url:
        content_lines.append(f"**PR:** {state.pr_url}")
    content = "\n".join(content_lines)
    try:
        client.post_discord_message(state.agent_config_id, content)
    except ApiError as exc:
        _log.warning("Discord failure notification failed: %s", exc, extra={"source": "cli"})


def _send_heartbeat(
    client: ApiClient,
    tick_id: int,
    *,
    tokens: int | None = None,
    cost: float | None = None,
) -> None:
    """Best-effort heartbeat ping."""
    try:
        client.tick_heartbeat(tick_id, token_cost_estimate=tokens, cost_usd=cost)
    except ApiError as exc:
        _log.warning("heartbeat failed: %s", exc, extra={"source": "cli"})


def _flush_pending_setup_steps(
    client: ApiClient,
    tick_id: int,
    pending: list[_PendingLifecycleStep],
) -> int:
    """POST back-dated setup rows now that the tick exists.

    Returns the next ordinal the caller should use (so agent rows continue
    sequentially). Failures here are logged but don't abort the tick — the
    CLI work already happened; a missing timeline row is a display defect.
    """
    for ordinal, step in enumerate(pending):
        try:
            opened = client.open_step(
                tick_id=tick_id,
                kind=step.kind,
                name=step.name,
                ordinal=ordinal,
                action=step.action,
                started_at=step.started_at,
            )
        except ApiError as exc:
            _log.warning("could not open %s step: %s", step.name, exc, extra={"source": "cli"})
            continue
        try:
            client.close_step(
                opened["id"],
                summary=step.summary,
                error_log=step.error_log,
                ended_at=step.ended_at,
            )
        except ApiError as exc:
            _log.warning("could not close %s step: %s", step.name, exc, extra={"source": "cli"})
    return len(pending)


_CLOSE_STEP_RETRY_ATTEMPTS = 3
_CLOSE_STEP_RETRY_BACKOFF_SECONDS = 1.5


def _close_step_with_retry(
    client: ApiClient,
    step_id: int,
    *,
    name: str,
    summary: str,
    error_log: str,
    cost_usd: float | None = None,
    token_cost_estimate: int | None = None,
    ended_at: datetime | None = None,
) -> bool:
    """Close a TickStep, retrying transient failures (e.g. 502 from upstream).

    A dropped close leaves the step "open" forever; the server then back-fills
    it with a generic "step left open when tick closed" error during tick
    close, masking the real outcome. Retrying a few times with a small backoff
    covers proxy / cold-start blips without blocking the tick.
    """
    kwargs: dict[str, Any] = {"summary": summary, "error_log": error_log}
    if cost_usd is not None:
        kwargs["cost_usd"] = cost_usd
    if token_cost_estimate is not None:
        kwargs["token_cost_estimate"] = token_cost_estimate
    kwargs["ended_at"] = ended_at or _utcnow()

    last_exc: ApiError | None = None
    for attempt in range(1, _CLOSE_STEP_RETRY_ATTEMPTS + 1):
        try:
            client.close_step(step_id, **kwargs)
        except ApiError as exc:
            last_exc = exc
            if attempt < _CLOSE_STEP_RETRY_ATTEMPTS:
                time.sleep(_CLOSE_STEP_RETRY_BACKOFF_SECONDS * attempt)
        else:
            return True

    _log.warning(
        "could not close %s step after %d attempts: %s",
        name,
        _CLOSE_STEP_RETRY_ATTEMPTS,
        last_exc,
        extra={"source": "cli"},
    )
    return False


def _run_lifecycle_step(
    client: ApiClient,
    *,
    tick_id: int,
    kind: str,
    name: str,
    ordinal: int,
    action: str,
    work: Callable[[], str],
) -> tuple[bool, str]:
    """Wrap a single post-tick-open CLI phase as a live TickStep.

    ``work`` runs the actual CLI command and returns a human-readable
    summary. Exceptions are caught, recorded on the step's ``error_log``,
    and returned as ``(False, str(exc))``. Cleanup callers treat failures
    as best-effort so the tick outcome isn't flipped by a teardown hiccup.
    """
    started_at = _utcnow()
    try:
        opened = client.open_step(
            tick_id=tick_id,
            kind=kind,
            name=name,
            ordinal=ordinal,
            action=action,
            started_at=started_at,
        )
    except ApiError as exc:
        _log.warning("could not open %s step: %s", name, exc, extra={"source": "cli"})
        try:
            summary = work()
        except Exception as work_exc:  # noqa: BLE001
            return False, str(work_exc)
        return True, summary

    step_id = opened["id"]
    try:
        summary = work()
        ok = True
        error_log = ""
    except Exception as exc:  # noqa: BLE001
        summary = ""
        error_log = str(exc)
        ok = False

    _close_step_with_retry(
        client,
        step_id,
        name=name,
        summary=summary,
        error_log=error_log,
    )
    return ok, summary if ok else error_log


def _execute_steps(  # noqa: PLR0911
    client: ApiClient,
    state: _TickState,
    steps: list[dict[str, Any]],
    repo_checkout: Path,
    shutdown_requested: dict[str, bool],
    storage: RepoStorage,
    *,
    start_ordinal: int,
) -> int:
    """Run agent steps. Returns the ordinal immediately after the last one.

    After each successful agent step, every tool listed on that step is
    dispatched as its own ``KIND_TOOL`` step (see :func:`_run_tool_steps`).
    Tool failures are non-fatal; only token exhaustion bubbles up.
    """
    ordinal = start_ordinal - 1
    for step in steps:
        ordinal += 1
        if shutdown_requested["value"]:
            state.status = STATUS_ABANDONED
            state.error = "client received shutdown signal"
            _log.warning("shutdown requested; abandoning tick", extra={"source": "cli"})
            return ordinal
        _send_heartbeat(client, state.tick_id, tokens=state.total_tokens, cost=state.total_cost)
        agent = step["agent_slug"]
        prompt = step.get("prompt") or ""
        display_name = step.get("display_name") or agent
        try:
            opened = client.open_step(
                tick_id=state.tick_id,
                kind=KIND_AGENT,
                agent_slug=agent,
                ordinal=ordinal,
                name=display_name,
                action=prompt[:_PROMPT_ACTION_CHARS],
            )
        except ApiError as exc:
            state.status = STATUS_FAILED
            state.error = f"step_open {agent} -> {exc}"
            _log.error("step open failed for %s: %s", agent, exc, extra={"source": "cli"})
            return ordinal
        step_id = opened["id"]
        _log.info("[cyan]→[/cyan] %s", agent, extra={"source": "cli", "step_id": step_id})
        storage.write_step_prompt(state.tick_id, step_id, prompt)
        storage.append_history(
            {
                "event": "step_open",
                "tick_id": state.tick_id,
                "step_id": step_id,
                "agent": agent,
                "ordinal": ordinal,
            },
        )
        result = run_step(
            prompt,
            cwd=repo_checkout,
            step_id=step_id,
            env=_step_env(client, step, tick_id=state.tick_id),
        )
        storage.write_step_streams(state.tick_id, step_id, stdout=result.stdout, stderr=result.stderr)
        state.total_cost += result.total_cost_usd
        state.total_tokens += result.token_cost_estimate
        # `result.summary` is already a one-line takeaway capped for dashboard display.
        # Showing it on both success and failure keeps the Steps table informative
        # (e.g. the bail reason) without leaking the raw JSON blob we used to tail.
        summary = result.summary
        if result.ok:
            error_log = ""
        elif result.fail_reason:
            error_log = result.fail_reason
        else:
            error_log = (result.stderr or result.stdout)[-_ERROR_CHARS:]
        storage.append_history(
            {
                "event": "step_closed",
                "tick_id": state.tick_id,
                "step_id": step_id,
                "agent": agent,
                "ok": result.ok,
                "cost_usd": result.total_cost_usd,
                "tokens": result.token_cost_estimate,
                "duration_ms": result.duration_ms,
            },
        )
        if not _close_step_with_retry(
            client,
            step_id,
            name=f"agent::{agent}",
            summary=summary,
            error_log=error_log,
            cost_usd=result.total_cost_usd,
            token_cost_estimate=result.token_cost_estimate,
        ):
            state.status = STATUS_FAILED
            state.error = f"step_close {agent} -> exhausted retries"
            return ordinal
        if result.token_exhausted:
            state.status = STATUS_TOKEN_EXHAUSTED
            state.error = "Claude subscription out of tokens."
            _log.error(
                "agent %s hit token exhaustion; pausing tick (not counted against retries)",
                agent,
                extra={"source": "cli", "step_id": step_id},
            )
            return ordinal
        if not result.ok:
            state.status = STATUS_FAILED
            state.error = error_log
            _log.error("agent %s failed (rc != 0)", agent, extra={"source": "cli", "step_id": step_id})
            return ordinal
        state.outcomes.append(f"{agent}: ok")
        step_tool_refs = [t for t in (step.get("tools") or []) if t.get("slug")]
        if step_tool_refs:
            ordinal = _run_tool_steps(
                client,
                state,
                step_tool_refs,
                repo_checkout,
                shutdown_requested,
                storage,
                parent_step=step,
                parent_result=result,
                start_ordinal=ordinal + 1,
            )
            if state.status == STATUS_TOKEN_EXHAUSTED:
                return ordinal
    return ordinal


def _resolve_tool_command(storage: RepoStorage, slug: str) -> str:
    """Return the slash command name to invoke for ``slug``.

    Reads the cached manifest body (written during tool reconcile) and
    returns the first command name. Falls back to the slug itself when no
    manifest is on disk yet (e.g. cached install from a previous CLI version
    that didn't persist manifests). The slash prefix is added by the caller.
    """
    body = storage.read_tool_manifest(slug)
    if body:
        commands = body.get("commands") or []
        if commands and isinstance(commands, list):
            first = commands[0]
            if isinstance(first, dict):
                name = first.get("name")
                if isinstance(name, str) and name:
                    return name
    return slug


def _build_tool_prompt(*, command: str, agent_slug: str, summary: str, stdout: str) -> str:
    """Wrap a slash command with the parent agent step's summary + stdout."""
    truncated = stdout
    if len(truncated) > _TOOL_STDOUT_MAX_CHARS:
        truncated = "[truncated]\n" + truncated[-_TOOL_STDOUT_MAX_CHARS:]
    return f"/{command}\n\nPrevious step: {agent_slug}\nSummary: {summary or '(no summary)'}\n\n--- stdout ---\n{truncated}\n"


def _run_tool_steps(
    client: ApiClient,
    state: _TickState,
    tool_refs: list[dict[str, Any]],
    repo_checkout: Path,
    shutdown_requested: dict[str, bool],
    storage: RepoStorage,
    *,
    parent_step: dict[str, Any],
    parent_result: ClaudeResult,
    start_ordinal: int,
) -> int:
    """Run one tool step per active tool against the parent agent step's output.

    Each tool gets its own ``KIND_TOOL`` TickStep on the server. We spawn
    ``claude -p`` with the tool's slash command, feeding it the parent
    agent's summary + stdout. Tool failures are logged but don't fail the
    tick (Discord posting is informational, not load-bearing). Token
    exhaustion is the only condition that bubbles up.

    Returns the last ordinal consumed (the last tool step's ordinal, or
    ``start_ordinal - 1`` if no tools ran).
    """
    agent_slug = parent_step.get("agent_slug") or ""
    ordinal = start_ordinal - 1
    for ref in tool_refs:
        if shutdown_requested["value"]:
            return ordinal
        slug = str(ref["slug"])
        ordinal += 1
        command = _resolve_tool_command(storage, slug)
        action = f"/{command}"
        try:
            opened = client.open_step(
                tick_id=state.tick_id,
                kind=KIND_TOOL,
                agent_slug=agent_slug,
                ordinal=ordinal,
                name=f"{agent_slug}::{slug}" if agent_slug else slug,
                action=action,
            )
        except ApiError as exc:
            _log.warning(
                "tool step open failed for %s: %s",
                slug,
                exc,
                extra={"source": "cli"},
            )
            continue
        tool_step_id = opened["id"]
        prompt = _build_tool_prompt(
            command=command,
            agent_slug=agent_slug,
            summary=parent_result.summary,
            stdout=parent_result.stdout,
        )
        storage.write_step_prompt(state.tick_id, tool_step_id, prompt)
        _log.info("[cyan]→[/cyan] tool %s", slug, extra={"source": "cli", "step_id": tool_step_id})
        result = run_step(
            prompt,
            cwd=repo_checkout,
            step_id=tool_step_id,
            env=_step_env(client, parent_step, tick_id=state.tick_id),
        )
        storage.write_step_streams(state.tick_id, tool_step_id, stdout=result.stdout, stderr=result.stderr)
        state.total_cost += result.total_cost_usd
        state.total_tokens += result.token_cost_estimate
        if result.ok:
            error_log = ""
        elif result.fail_reason:
            error_log = result.fail_reason
        else:
            error_log = (result.stderr or result.stdout)[-_ERROR_CHARS:]
        _close_step_with_retry(
            client,
            tool_step_id,
            name=f"tool::{slug}",
            summary=result.summary,
            error_log=error_log,
            cost_usd=result.total_cost_usd,
            token_cost_estimate=result.token_cost_estimate,
        )
        if result.token_exhausted:
            state.status = STATUS_TOKEN_EXHAUSTED
            state.error = "Claude subscription out of tokens."
            _log.error(
                "tool %s hit token exhaustion; pausing tick",
                slug,
                extra={"source": "cli", "step_id": tool_step_id},
            )
            return ordinal
        if not result.ok:
            _log.warning(
                "tool %s failed (rc != 0); continuing tick",
                slug,
                extra={"source": "cli", "step_id": tool_step_id},
            )
    return ordinal


def _reconcile_tools(client: ApiClient, tool_refs: list[dict[str, Any]], *, storage: RepoStorage) -> int:
    """Apply any server-advertised tool manifest whose hash differs from the local cache.

    Returns the number of manifests that were applied (for the setup step
    summary). Failures to fetch a single manifest are logged and skipped.
    """
    if not tool_refs:
        return 0
    refs = [ManifestRef.from_dict(r) for r in tool_refs if r.get("slug")]
    if not refs:
        return 0
    cached = storage.read_tool_hashes()
    drifted = [ref for ref in refs if cached.get(ref.slug) != ref.manifest_hash]
    if not drifted:
        return 0
    home = Path.home()
    new_cache = dict(cached)
    applied = 0
    for ref in drifted:
        try:
            payload = client.get_tool_manifest(ref.slug)
        except ApiError as exc:
            _log.warning("tool manifest fetch failed for %s: %s", ref.slug, exc, extra={"source": "cli"})
            continue
        manifest = Manifest.from_payload(
            slug=ref.slug,
            manifest_hash=str(payload.get("manifest_hash") or ref.manifest_hash),
            body=payload.get("manifest") or {},
        )
        touched = apply_manifest(home, manifest)
        # Persist the manifest body so later phases (e.g. _run_tool_steps) can
        # look up the slash command name for the tool without re-fetching.
        storage.write_tool_manifest(ref.slug, payload.get("manifest") or {})
        new_cache[ref.slug] = manifest.manifest_hash
        applied += 1
        _log.info(
            "[green]tool %s installed[/green] (%d files)",
            ref.slug,
            len(touched),
            extra={"source": "cli"},
        )
    storage.write_tool_hashes(new_cache)
    return applied


def _persist_tick_outcome(
    storage: RepoStorage,
    state: _TickState,
    *,
    started_at: str,
) -> None:
    """Write ``state/last_tick.json`` and ``logs/ticks/<id>/summary.json`` after close."""
    summary = {
        "tick_id": state.tick_id,
        "status": state.status,
        "outcomes": state.outcomes,
        "error": state.error,
        "cost_usd": state.total_cost,
        "tokens": state.total_tokens,
        "started_at": started_at,
        "ended_at": _iso_now(),
        "cli_version": __version__,
    }
    storage.write_last_tick(summary)
    storage.write_tick_summary(state.tick_id, summary)
    storage.append_history({"event": "tick_closed", **summary})


_GITHUB_NAME_DISALLOWED = re.compile(r"[^A-Za-z0-9._-]+")
_GITHUB_NAME_MAX_LEN = 100
_AUTOCREATE_MAX_ATTEMPTS = 100
_AUTOCREATE_FALLBACK_NAME = "autoclaude-project"


def _slugify_for_github(name: str) -> str:
    """Coerce a project name into a GitHub-legal repo name.

    GitHub allows alphanumerics, dots, underscores, and hyphens. We replace
    every other character (and runs of whitespace) with a hyphen, collapse
    repeats, trim leading/trailing hyphens, and lowercase. Empty results
    fall back to a constant rather than letting `gh repo create` reject an
    empty name with a confusing message.
    """
    base = _GITHUB_NAME_DISALLOWED.sub("-", name.strip())
    base = re.sub(r"-+", "-", base).strip("-").lower()
    base = base[:_GITHUB_NAME_MAX_LEN].rstrip("-.")
    return base or _AUTOCREATE_FALLBACK_NAME


def _find_available_repo_name(owner: str, base: str, *, max_attempts: int = _AUTOCREATE_MAX_ATTEMPTS) -> str:
    """Return the first ``base[-N]`` name not already taken on GitHub.

    Tries ``base``, ``base-1``, ``base-2``, ..., up to ``max_attempts``.
    Probing uses ``gh repo view`` so we hit GitHub directly rather than
    racing on cached state. The cap exists so a misconfigured org with
    thousands of stale repos can't push us into an infinite loop; in
    practice a free user will collide on -0..-3 at most.
    """
    for index in range(max_attempts):
        candidate = base if index == 0 else f"{base}-{index}"
        if not gh_helpers.repo_exists(f"{owner}/{candidate}"):
            return candidate
    msg = f"all suffixes 0..{max_attempts - 1} are taken for base name {base!r} under {owner}"
    raise GhError(msg)


def _autocreate_github_repo(client: ApiClient, project: dict[str, Any]) -> str:
    """Create the project's GitHub repo and persist it on the server.

    Used when ``project.github_repo`` comes back empty from
    ``client.context()``. Picks ``<authed-user>/<slugified-project-name>``
    (incrementing a numeric suffix on collision), creates the repo as
    private via ``gh``, then PATCHes the server so future ticks resolve
    the same value without re-running this branch.
    """
    project_id = project.get("id")
    project_name = project.get("name") or _AUTOCREATE_FALLBACK_NAME
    # DEBUG(autocreate-no-id): temporary diagnostic logging for "context project has no id" issue. Remove once root cause confirmed.
    _log.info(
        "autocreate: project_id=%r (type=%s) name=%r keys=%s full_dict=%r",
        project_id,
        type(project_id).__name__,
        project_name,
        sorted(project.keys()),
        project,
        extra={"source": "cli"},
    )
    if not isinstance(project_id, int):
        # DEBUG(autocreate-no-id): error enriched with project_id type + full dict.
        # Trim back to short message once issue resolved.
        msg = (
            "context project has no id; cannot auto-create github repo. "
            f"got project_id={project_id!r} (type={type(project_id).__name__}); "
            f"full project dict={project!r}"
        )
        raise GhError(msg)

    owner = gh_helpers.current_user_login()
    base = _slugify_for_github(project_name)
    name = _find_available_repo_name(owner, base)
    full_repo = f"{owner}/{name}"
    _log.info(
        "[yellow]project has no github_repo; creating[/yellow] %s (private)",
        full_repo,
        extra={"source": "cli"},
    )
    gh_helpers.repo_create(full_repo, private=True)
    client.update_project_github_repo(project_id, full_repo)
    _log.info(
        "[green]github_repo set on project %s ->[/green] %s",
        project_id,
        full_repo,
        extra={"source": "cli"},
    )
    return full_repo


def _cleanup_worktree(workspace: Workspace, worktree: Worktree) -> None:
    """Remove the worktree."""
    try:
        workspace.remove_worktree(worktree)
    except WorkspaceError as exc:
        _log.warning("worktree cleanup failed: %s", exc, extra={"source": "cli"})


def run_tick(client: ApiClient, *, workspace_factory: Callable[[str], Workspace] | None = None) -> int:  # noqa: PLR0911
    """Fire one tick against the project's GitHub repo using an isolated worktree.

    The workspace is built from ``project.github_repo`` returned by the
    server's runner context, so the local clone always matches the repo
    the agent will issue ``gh`` against. ``workspace_factory`` is a
    test-only seam: production calls ``Workspace.for_github_repo``.

    Captures wall-clock timings for the pre-tick-open phases (context
    fetch, repo sync, storage prep) so they can be replayed as
    ``TickStep`` rows once the tick exists on the server.
    """
    try:
        ensure_gh_installed()
    except GhError as exc:
        _log.error("[red]%s[/red]", exc, extra={"source": "cli"})
        return EXIT_FAILED

    pending: list[_PendingLifecycleStep] = []

    try:
        ctx = client.context()
    except ApiError as exc:
        _log.error("[red]context fetch failed[/red]: %s", exc, extra={"source": "cli"})
        return EXIT_FAILED

    schedule = ctx.get("tick_schedule") or {}
    if schedule and schedule.get("eligible_now") is False:
        next_eligible_at = schedule.get("next_eligible_at") or "?"
        interval_minutes = schedule.get("interval_minutes")
        _log.info(
            "[dim]scheduled tick skipped[/dim]: server interval %s min, next eligible at %s",
            interval_minutes,
            next_eligible_at,
            extra={"source": "cli"},
        )
        return EXIT_OK

    # DEBUG(autocreate-no-id): temporary diagnostic logging for "context project has no id" issue. Remove once root cause confirmed.
    _log.debug(
        "context payload keys=%s project=%r team=%r plan_steps=%s",
        sorted(ctx.keys()),
        ctx.get("project"),
        ctx.get("team"),
        len((ctx.get("plan") or {}).get("steps") or []),
        extra={"source": "cli"},
    )
    project = ctx.get("project") or {}
    if not project:
        _log.info("[dim]scheduled tick skipped[/dim]: no project available", extra={"source": "cli"})
        return EXIT_OK
    github_repo = project.get("github_repo") or ""
    if not github_repo:
        # DEBUG(autocreate-no-id): temporary diagnostic logging. Remove once root cause confirmed.
        _log.warning(
            "project has empty github_repo; entering autocreate. project_keys=%s id=%r name=%r default_branch=%r raw=%r",
            sorted(project.keys()),
            project.get("id"),
            project.get("name"),
            project.get("default_branch"),
            project,
            extra={"source": "cli"},
        )
        try:
            github_repo = _autocreate_github_repo(client, project)
        except (GhError, ApiError) as exc:
            # DEBUG(autocreate-no-id): extra context (project dict, ctx keys) added for diagnostics. Trim back to just `exc` once issue resolved.
            _log.error(
                "[red]github repo auto-create failed[/red]: %s | project=%r ctx_keys=%s",
                exc,
                project,
                sorted(ctx.keys()),
                extra={"source": "cli"},
            )
            return EXIT_FAILED

    factory = workspace_factory or Workspace.for_github_repo
    repo_sync_started = _utcnow()
    try:
        workspace = factory(github_repo)
        workspace.sync()
    except WorkspaceError as exc:
        _log.error("[red]workspace sync failed[/red]: %s", exc, extra={"source": "cli"})
        return EXIT_FAILED
    pending.append(
        _PendingLifecycleStep(
            name=STEP_REPO_SYNC,
            kind=KIND_SETUP,
            started_at=repo_sync_started,
            ended_at=_utcnow(),
            action=f"Workspace.for_github_repo({github_repo!r}) + workspace.sync()",
            summary=f"workspace cloned from {workspace.clone_url} at {workspace.clone_path}",
        ),
    )

    storage_started = _utcnow()
    storage = RepoStorage.from_repo(workspace.clone_path)
    storage.ensure()
    cfg = repo_config_mod.load(workspace.clone_path)
    storage.prune(cfg.retention)
    storage.clean_tmp()
    pending.append(
        _PendingLifecycleStep(
            name=STEP_STORAGE_PREP,
            kind=KIND_SETUP,
            started_at=storage_started,
            ended_at=_utcnow(),
            action="storage.ensure() + storage.prune(retention) + storage.clean_tmp()",
            summary=f".autoclaude/ ready (retention={cfg.retention})",
        ),
    )

    return _run_tick_body(client, ctx=ctx, workspace=workspace, storage=storage, pending=pending)


def _run_tick_body(  # noqa: C901, PLR0911, PLR0912, PLR0915
    client: ApiClient,
    *,
    ctx: dict[str, Any],
    workspace: Workspace,
    storage: RepoStorage,
    pending: list[_PendingLifecycleStep],
) -> int:
    plan = ctx.get("plan")
    if plan is None or not plan.get("steps"):
        _log.warning("[yellow]no active job; nothing to do[/yellow]", extra={"source": "cli"})
        return EXIT_OK

    tool_reconcile_started = _utcnow()
    applied_tool_count = _reconcile_tools(client, plan.get("tools") or [], storage=storage)
    pending.append(
        _PendingLifecycleStep(
            name=STEP_TOOL_RECONCILE,
            kind=KIND_SETUP,
            started_at=tool_reconcile_started,
            ended_at=_utcnow(),
            action="_reconcile_tools(plan.tools)",
            summary=(f"{applied_tool_count} tool manifest(s) applied" if applied_tool_count else "no tool manifests drifted"),
        ),
    )

    try:
        tick = client.open_tick(runner_version=__version__)
    except ApiError as exc:
        if exc.status_code == 425:
            payload = exc.payload if isinstance(exc.payload, dict) else {}
            sched = payload.get("tick_schedule") or {}
            _log.info(
                "[dim]scheduled tick skipped[/dim]: server interval %s min, next eligible at %s",
                sched.get("interval_minutes"),
                sched.get("next_eligible_at") or "?",
                extra={"source": "cli"},
            )
            return EXIT_OK
        if exc.status_code == 409:
            payload = exc.payload if isinstance(exc.payload, dict) else {}
            _log.info(
                "[dim]tick skipped[/dim]: %s",
                payload.get("reason") or payload.get("code") or "another tick already running",
                extra={"source": "cli"},
            )
            return EXIT_LOCKED
        _log.error("[red]tick open failed[/red]: %s", exc, extra={"source": "cli"})
        return EXIT_FAILED

    steps = list(tick.get("plan", {}).get("steps") or plan["steps"])
    resumed_from = tick.get("resumed_from")
    if resumed_from:
        _log.info(
            "[yellow]resuming abandoned tick #%s[/yellow]",
            resumed_from.get("tick_id"),
            extra={"source": "cli"},
        )
        _apply_resumption(steps, resumed_from)

    state = _TickState(tick_id=tick["id"])
    state.agent_config_id = next(
        (s.get("agent_config_id") for s in steps if s.get("agent_config_id") is not None),
        None,
    )
    started_at_iso = _iso_now()
    storage.append_history({"event": "tick_open", "tick_id": state.tick_id, "resumed_from": resumed_from})
    _log.info("[green]tick #%s open[/green]", state.tick_id, extra={"source": "cli"})

    # Flush the pre-tick-open setup rows as back-dated TickSteps.
    setup_count = _flush_pending_setup_steps(client, state.tick_id, pending)

    # workspace_prep is the first post-tick-open setup phase.
    worktree: Worktree | None = None

    base_branch_input = str(plan.get("base_branch") or "").strip()
    base_ref = f"origin/{base_branch_input}" if base_branch_input else "HEAD"
    auto_merge = bool(plan.get("auto_merge"))

    def _do_worktree() -> str:
        nonlocal worktree
        worktree = workspace.create_worktree(state.tick_id, base=base_ref)
        return f"worktree at {worktree.path} on branch {worktree.branch} (base={base_ref})"

    ok, detail = _run_lifecycle_step(
        client,
        tick_id=state.tick_id,
        kind=KIND_SETUP,
        name=STEP_WORKSPACE_PREP,
        ordinal=setup_count,
        action=f"workspace.create_worktree(tick_id, base={base_ref!r})",
        work=_do_worktree,
    )
    if not ok or worktree is None:
        state.status = STATUS_FAILED
        state.error = f"workspace_prep -> {detail}"
        try:
            client.close_tick(
                state.tick_id,
                status=state.status,
                outcome="",
                error_log=state.error,
                cost_usd=0.0,
                token_cost_estimate=0,
            )
        except ApiError as close_exc:
            _log.error("tick close failed after workspace_prep error: %s", close_exc, extra={"source": "cli"})
        _persist_tick_outcome(storage, state, started_at=started_at_iso)
        _notify_tick_failure(client, state)
        return EXIT_FAILED

    agent_start_ordinal = setup_count + 1

    shutdown_requested: dict[str, bool] = {"value": False}

    def _handler(signum: int, _frame: object) -> None:  # noqa: ARG001
        shutdown_requested["value"] = True

    prev_int = signal.signal(signal.SIGINT, _handler)
    prev_term = signal.signal(signal.SIGTERM, _handler)

    worktree_cleaned = False
    try:
        with (
            TickLogger(client, state.tick_id, repo_checkout=worktree.path) as tick_logger,
            HeartbeatPinger(
                client,
                state.tick_id,
                get_totals=lambda: (state.total_tokens, state.total_cost),
            ),
        ):
            _send_heartbeat(client, state.tick_id, tokens=state.total_tokens, cost=state.total_cost)
            last_agent_ordinal = _execute_steps(
                client,
                state,
                steps,
                worktree.path,
                shutdown_requested,
                storage,
                start_ordinal=agent_start_ordinal,
            )

            cleanup_base = last_agent_ordinal + 1 if steps else agent_start_ordinal

            # Skip the branch_push + pr_open cleanup steps entirely when the
            # worktree has no commits beyond base. Surfacing them as "done
            # (skipped)" rows is noise: nothing was pushed, no PR is openable.
            has_commits = workspace.commits_ahead(worktree.path, base_ref) > 0

            if has_commits:

                def _do_branch_push() -> str:
                    # Push the worktree branch so its URL is available when
                    # `_do_finalize` writes the tick summary, and so any agent
                    # comments referencing the URL resolve immediately. Best-effort:
                    # a push hiccup must not flip the tick to failed, since the
                    # work itself is already committed locally.
                    url = workspace.push_branch(worktree.branch)
                    state.branch_url = url
                    return f"pushed {worktree.branch} -> {url}"

                _run_lifecycle_step(
                    client,
                    tick_id=state.tick_id,
                    kind=KIND_CLEANUP,
                    name=STEP_BRANCH_PUSH,
                    ordinal=cleanup_base,
                    action=f"git push -u origin {worktree.branch}",
                    work=_do_branch_push,
                )

            if has_commits:

                def _do_pr_open() -> str:
                    # Open a PR from the worktree branch back into the tick's base
                    # branch. Best-effort: skip when no base is declared (nothing
                    # sensible to target) or when push did not succeed. A failed
                    # PR open must not flip the tick to failed -- the work is
                    # already pushed.
                    if not base_branch_input:
                        return "skipped: no base_branch declared"
                    if not state.branch_url:
                        return "skipped: branch_url unset (push did not succeed)"
                    try:
                        pr_url = gh_helpers.pr_create(
                            base=base_branch_input,
                            head=worktree.branch,
                            cwd=worktree.path,
                        )
                    except GhError as exc:
                        return f"skipped: {exc}"
                    state.pr_url = pr_url
                    outcome = f"opened PR {worktree.branch} -> {base_branch_input}: {pr_url}"
                    if auto_merge:
                        try:
                            gh_helpers.pr_merge(
                                pr_url=pr_url,
                                cwd=worktree.path,
                                method="squash",
                                delete_branch=True,
                            )
                        except GhError as exc:
                            return f"{outcome}; merge skipped: {exc}"
                        outcome += " (merged + branch deleted)"
                    return outcome

                _run_lifecycle_step(
                    client,
                    tick_id=state.tick_id,
                    kind=KIND_CLEANUP,
                    name=STEP_PR_OPEN,
                    ordinal=cleanup_base + 1,
                    action=(
                        f"gh pr create --base {base_branch_input} --head {worktree.branch} --fill"
                        + (" && gh pr merge --squash --delete-branch" if auto_merge else "")
                    ),
                    work=_do_pr_open,
                )

            def _do_finalize() -> str:
                # Upload the file-tree snapshot before closing so the dashboard
                # always has a layout for the terminal tick. Failures here are
                # best-effort: a missing tree is a display defect, not a reason
                # to flip the tick's outcome.
                snapshot = build_file_tree_snapshot(storage)
                if snapshot is not None:
                    try:
                        client.upload_tick_file_tree(state.tick_id, snapshot)
                    except ApiError as exc:
                        _log.warning("file tree upload failed: %s", exc, extra={"source": "cli"})
                outcome_lines = list(state.outcomes)
                if state.branch_url:
                    outcome_lines.append(f"branch: {state.branch_url}")
                if state.pr_url:
                    outcome_lines.append(f"pr: {state.pr_url}")
                client.close_tick(
                    state.tick_id,
                    status=state.status,
                    outcome="\n".join(outcome_lines),
                    error_log=state.error,
                    cost_usd=state.total_cost,
                    token_cost_estimate=state.total_tokens,
                )
                _persist_tick_outcome(storage, state, started_at=started_at_iso)
                return f"tick closed with status={state.status}"

            _run_lifecycle_step(
                client,
                tick_id=state.tick_id,
                kind=KIND_CLEANUP,
                name=STEP_FINALIZE,
                ordinal=cleanup_base + 2,
                action="close_tick + write last_tick.json + summary.json",
                work=_do_finalize,
            )

            def _do_workspace_cleanup() -> str:
                nonlocal worktree_cleaned
                _cleanup_worktree(workspace, worktree)
                worktree_cleaned = True
                return f"worktree at {worktree.path} removed"

            _run_lifecycle_step(
                client,
                tick_id=state.tick_id,
                kind=KIND_CLEANUP,
                name=STEP_WORKSPACE_CLEANUP,
                ordinal=cleanup_base + 3,
                action=f"workspace.remove_worktree({worktree.path})",
                work=_do_workspace_cleanup,
            )

            def _do_log_flush() -> str:
                tick_logger.flush(timeout=_LOG_FLUSH_TIMEOUT)
                return "pending log buffer flushed"

            _run_lifecycle_step(
                client,
                tick_id=state.tick_id,
                kind=KIND_CLEANUP,
                name=STEP_LOG_FLUSH,
                ordinal=cleanup_base + 4,
                action=f"TickLogger.flush(timeout={_LOG_FLUSH_TIMEOUT})",
                work=_do_log_flush,
            )
    finally:
        signal.signal(signal.SIGINT, prev_int)
        signal.signal(signal.SIGTERM, prev_term)
        if not worktree_cleaned and worktree is not None:
            _cleanup_worktree(workspace, worktree)

    _log.info(
        "[green]tick #%s closed[/green] status=%s cost=$%.4f tokens=%s",
        state.tick_id,
        state.status,
        state.total_cost,
        state.total_tokens,
        extra={"source": "cli"},
    )
    _notify_tick_failure(client, state)
    if state.status == STATUS_SUCCEEDED:
        return EXIT_OK
    if state.status == STATUS_TOKEN_EXHAUSTED:
        return EXIT_TOKEN_EXHAUSTED
    if state.status == STATUS_ABANDONED:
        return EXIT_ABANDONED
    return EXIT_FAILED
