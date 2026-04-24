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

import signal
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from filelock import Timeout

from autoclaude import __version__
from autoclaude import repo_config as repo_config_mod
from autoclaude.api_client import ApiClient, ApiError
from autoclaude.claude_proc import run_step
from autoclaude.debug_files import fulfill_pending as fulfill_debug_requests
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
KIND_CLEANUP = "cleanup"

STEP_REPO_SYNC = "repo_sync"
STEP_STORAGE_PREP = "storage_prep"
STEP_TOOL_RECONCILE = "tool_reconcile"
STEP_WORKSPACE_PREP = "workspace_prep"
STEP_FINALIZE = "finalize"
STEP_WORKSPACE_CLEANUP = "workspace_cleanup"
STEP_LOG_FLUSH = "log_flush"

# Exit codes consumed by cli.py so `autoclaude tick` callers can distinguish
# billing failures from generic failures from graceful shutdowns.
EXIT_OK = 0
EXIT_FAILED = 1
EXIT_TOKEN_EXHAUSTED = 3
EXIT_ABANDONED = 130
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


def _send_heartbeat(
    client: ApiClient,
    tick_id: int,
    *,
    tokens: int | None = None,
    cost: float | None = None,
    storage: RepoStorage | None = None,
) -> None:
    """Best-effort heartbeat ping + debug-file poll."""
    try:
        client.tick_heartbeat(tick_id, token_cost_estimate=tokens, cost_usd=cost)
    except ApiError as exc:
        _log.warning("heartbeat failed: %s", exc, extra={"source": "cli"})
    if storage is not None:
        fulfill_debug_requests(client, storage)


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

    try:
        client.close_step(
            step_id,
            summary=summary,
            error_log=error_log,
            ended_at=_utcnow(),
        )
    except ApiError as exc:
        _log.warning("could not close %s step: %s", name, exc, extra={"source": "cli"})
    return ok, summary if ok else error_log


def _execute_steps(
    client: ApiClient,
    state: _TickState,
    steps: list[dict[str, Any]],
    repo_checkout: Path,
    shutdown_requested: dict[str, bool],
    storage: RepoStorage,
    *,
    start_ordinal: int,
) -> int:
    """Run agent steps. Returns the ordinal immediately after the last one."""
    ordinal = start_ordinal
    for offset, step in enumerate(steps):
        ordinal = start_ordinal + offset
        if shutdown_requested["value"]:
            state.status = STATUS_ABANDONED
            state.error = "client received shutdown signal"
            _log.warning("shutdown requested; abandoning tick", extra={"source": "cli"})
            return ordinal
        _send_heartbeat(client, state.tick_id, tokens=state.total_tokens, cost=state.total_cost, storage=storage)
        agent = step["agent_slug"]
        prompt = step.get("prompt") or ""
        try:
            opened = client.open_step(
                tick_id=state.tick_id,
                kind=KIND_AGENT,
                agent_slug=agent,
                ordinal=ordinal,
                name=agent,
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
        result = run_step(prompt, cwd=repo_checkout, step_id=step_id)
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
        try:
            client.close_step(
                step_id,
                summary=summary,
                error_log=error_log,
                cost_usd=result.total_cost_usd,
                token_cost_estimate=result.token_cost_estimate,
            )
        except ApiError as exc:
            state.status = STATUS_FAILED
            state.error = f"step_close {agent} -> {exc}"
            _log.error("step close failed for %s: %s", agent, exc, extra={"source": "cli", "step_id": step_id})
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
    return start_ordinal + len(steps) - 1


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


def _cleanup_worktree(workspace: Workspace, worktree: Worktree) -> None:
    """Remove the tick's worktree, tolerating any teardown error."""
    try:
        workspace.remove_worktree(worktree)
    except WorkspaceError as exc:
        _log.warning("worktree cleanup failed: %s", exc, extra={"source": "cli"})


def run_tick(client: ApiClient, *, source_repo: Path) -> int:
    """Fire one tick against ``source_repo`` using an isolated worktree.

    Captures wall-clock timings for the pre-tick-open phases (repo sync,
    storage prep) so they can be replayed as ``TickStep`` rows once the
    tick exists on the server.
    """
    try:
        ensure_gh_installed()
    except GhError as exc:
        _log.error("[red]%s[/red]", exc, extra={"source": "cli"})
        return EXIT_FAILED

    pending: list[_PendingLifecycleStep] = []

    repo_sync_started = _utcnow()
    try:
        workspace = Workspace.for_source(source_repo)
        workspace.sync(source_repo)
    except WorkspaceError as exc:
        _log.error("[red]workspace sync failed[/red]: %s", exc, extra={"source": "cli"})
        return EXIT_FAILED
    pending.append(
        _PendingLifecycleStep(
            name=STEP_REPO_SYNC,
            kind=KIND_SETUP,
            started_at=repo_sync_started,
            ended_at=_utcnow(),
            action=f"Workspace.for_source({source_repo}) + workspace.sync(...)",
            summary=f"workspace cloned at {workspace.clone_path}",
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

    tick_lock = storage.tick_lock()
    try:
        tick_lock.acquire(timeout=0.0)
    except Timeout:
        _log.error(
            "[red]another autoclaude tick is already running for %s[/red]",
            workspace.clone_path,
            extra={"source": "cli"},
        )
        return EXIT_LOCKED

    try:
        return _run_tick_locked(client, workspace=workspace, storage=storage, pending=pending)
    finally:
        tick_lock.release()


def _run_tick_locked(  # noqa: PLR0911, PLR0912, PLR0915, C901 (exit-code dispatch + explicit step sequencing)
    client: ApiClient,
    *,
    workspace: Workspace,
    storage: RepoStorage,
    pending: list[_PendingLifecycleStep],
) -> int:
    try:
        ctx = client.context()
    except ApiError as exc:
        _log.error("[red]context fetch failed[/red]: %s", exc, extra={"source": "cli"})
        return EXIT_FAILED

    # Wire a `github` remote so the issuer agent's `gh issue list` resolves the
    # right repo; origin stays pinned to the user's local source for fetches.
    project = ctx.get("project") or {}
    github_repo = project.get("github_repo") or ""
    if github_repo:
        try:
            workspace.configure_github_remote(github_repo)
        except WorkspaceError as exc:
            _log.warning("github remote config failed: %s", exc, extra={"source": "cli"})

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
            summary=(
                f"{applied_tool_count} tool manifest(s) applied"
                if applied_tool_count
                else "no tool manifests drifted"
            ),
        ),
    )

    try:
        tick = client.open_tick(runner_version=__version__)
    except ApiError as exc:
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
    started_at_iso = _iso_now()
    storage.append_history({"event": "tick_open", "tick_id": state.tick_id, "resumed_from": resumed_from})
    _log.info("[green]tick #%s open[/green]", state.tick_id, extra={"source": "cli"})

    # Flush the pre-tick-open setup rows as back-dated TickSteps.
    setup_count = _flush_pending_setup_steps(client, state.tick_id, pending)

    # workspace_prep is the first post-tick-open setup phase.
    worktree: Worktree | None = None

    def _do_worktree() -> str:
        nonlocal worktree
        worktree = workspace.create_worktree(state.tick_id)
        return f"worktree at {worktree.path} on branch {worktree.branch}"

    ok, detail = _run_lifecycle_step(
        client,
        tick_id=state.tick_id,
        kind=KIND_SETUP,
        name=STEP_WORKSPACE_PREP,
        ordinal=setup_count,
        action="workspace.create_worktree(tick_id)",
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
            _send_heartbeat(client, state.tick_id, tokens=state.total_tokens, cost=state.total_cost, storage=storage)
            last_agent_ordinal = _execute_steps(
                client,
                state,
                steps,
                worktree.path,
                shutdown_requested,
                storage,
                start_ordinal=agent_start_ordinal,
            )

            cleanup_base = (
                last_agent_ordinal + 1 if steps else agent_start_ordinal
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
                client.close_tick(
                    state.tick_id,
                    status=state.status,
                    outcome="\n".join(state.outcomes),
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
                ordinal=cleanup_base,
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
                ordinal=cleanup_base + 1,
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
                ordinal=cleanup_base + 2,
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
    if state.status == STATUS_SUCCEEDED:
        return EXIT_OK
    if state.status == STATUS_TOKEN_EXHAUSTED:
        return EXIT_TOKEN_EXHAUSTED
    if state.status == STATUS_ABANDONED:
        return EXIT_ABANDONED
    return EXIT_FAILED
