"""Orchestrate one tick: fetch context, execute steps, close."""

from __future__ import annotations

import signal
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from filelock import Timeout

from autoclaude import __version__
from autoclaude import repo_config as repo_config_mod
from autoclaude.api_client import ApiClient, ApiError
from autoclaude.claude_proc import run_step
from autoclaude.debug_files import fulfill_pending as fulfill_debug_requests
from autoclaude.logger import get_logger
from autoclaude.storage import RepoStorage
from autoclaude.tick_logger import TickLogger
from autoclaude.tools.applier import apply_manifest
from autoclaude.tools.manifest import Manifest, ManifestRef
from autoclaude.workspace import Workspace, WorkspaceError, Worktree

_log = get_logger("runner")

_SUMMARY_CHARS = 1000
_ERROR_CHARS = 2000
_RESUMPTION_SUMMARY_MAX = 500

STATUS_SUCCEEDED = "succeeded"
STATUS_FAILED = "failed"
STATUS_TOKEN_EXHAUSTED = "token_exhausted"  # noqa: S105 (status label, not a secret)
STATUS_ABANDONED = "abandoned"

# Exit codes consumed by cli.py so `autoclaude tick` callers can distinguish
# billing failures from generic failures from graceful shutdowns.
EXIT_OK = 0
EXIT_FAILED = 1
EXIT_TOKEN_EXHAUSTED = 3
EXIT_ABANDONED = 130
EXIT_LOCKED = 4


@dataclass
class _TickState:
    tick_id: int
    total_cost: float = 0.0
    total_tokens: int = 0
    status: str = STATUS_SUCCEEDED
    error: str = ""
    outcomes: list[str] = field(default_factory=list)


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
    """Best-effort heartbeat ping + debug-file poll.

    Transient errors must not abort the tick. The debug-file poll is
    ``storage``-gated because some internal call sites do not have one
    handy; when supplied, pending file requests are fulfilled between
    heartbeats so the dashboard can read runner-local state live.
    """
    try:
        client.tick_heartbeat(tick_id, token_cost_estimate=tokens, cost_usd=cost)
    except ApiError as exc:
        _log.warning("heartbeat failed: %s", exc, extra={"source": "cli"})
    if storage is not None:
        fulfill_debug_requests(client, storage)


def _execute_steps(
    client: ApiClient,
    state: _TickState,
    steps: list[dict[str, Any]],
    repo_checkout: Path,
    shutdown_requested: dict[str, bool],
    storage: RepoStorage,
) -> None:
    for ordinal, step in enumerate(steps):
        if shutdown_requested["value"]:
            state.status = STATUS_ABANDONED
            state.error = "client received shutdown signal"
            _log.warning("shutdown requested; abandoning tick", extra={"source": "cli"})
            return
        _send_heartbeat(client, state.tick_id, tokens=state.total_tokens, cost=state.total_cost, storage=storage)
        agent = step["agent_slug"]
        try:
            opened = client.open_step(tick_id=state.tick_id, agent_slug=agent, ordinal=ordinal, name=agent)
        except ApiError as exc:
            state.status = STATUS_FAILED
            state.error = f"step_open {agent} -> {exc}"
            _log.error("step open failed for %s: %s", agent, exc, extra={"source": "cli"})
            return
        step_id = opened["id"]
        _log.info("[cyan]→[/cyan] %s", agent, extra={"source": "cli", "step_id": step_id})
        storage.write_step_prompt(state.tick_id, step_id, step["prompt"])
        storage.append_history({"event": "step_open", "tick_id": state.tick_id, "step_id": step_id, "agent": agent, "ordinal": ordinal})
        result = run_step(step["prompt"], cwd=repo_checkout, step_id=step_id)
        storage.write_step_streams(state.tick_id, step_id, stdout=result.stdout, stderr=result.stderr)
        state.total_cost += result.total_cost_usd
        state.total_tokens += result.token_cost_estimate
        summary = result.stdout[-_SUMMARY_CHARS:] if result.ok else ""
        error_log = "" if result.ok else (result.stderr or result.stdout)[-_ERROR_CHARS:]
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
            return
        if result.token_exhausted:
            state.status = STATUS_TOKEN_EXHAUSTED
            state.error = "Claude subscription out of tokens."
            _log.error(
                "agent %s hit token exhaustion; pausing tick (not counted against retries)",
                agent,
                extra={"source": "cli", "step_id": step_id},
            )
            return
        if not result.ok:
            state.status = STATUS_FAILED
            state.error = error_log
            _log.error("agent %s failed (rc != 0)", agent, extra={"source": "cli", "step_id": step_id})
            return
        state.outcomes.append(f"{agent}: ok")


def _reconcile_tools(client: ApiClient, tool_refs: list[dict[str, Any]], *, storage: RepoStorage) -> None:
    """Apply any server-advertised tool manifest whose hash differs from the local cache.

    Called once per tick before ``open_tick``. Failures to fetch a manifest
    are logged and skipped; they do not block the tick from opening.
    """
    if not tool_refs:
        return
    refs = [ManifestRef.from_dict(r) for r in tool_refs if r.get("slug")]
    if not refs:
        return
    cached = storage.read_tool_hashes()
    drifted = [ref for ref in refs if cached.get(ref.slug) != ref.manifest_hash]
    if not drifted:
        return
    home = Path.home()
    new_cache = dict(cached)
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
        _log.info(
            "[green]tool %s installed[/green] (%d files)",
            ref.slug,
            len(touched),
            extra={"source": "cli"},
        )
    storage.write_tool_hashes(new_cache)


def _iso_now() -> str:
    return datetime.now(tz=UTC).isoformat().replace("+00:00", "Z")


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
    """Remove the tick's worktree, tolerating any teardown error.

    Cleanup is best-effort; a failure here must not mask the tick outcome.
    The branch is preserved so the tick's changes remain inspectable.
    """
    try:
        workspace.remove_worktree(worktree)
    except WorkspaceError as exc:
        _log.warning("worktree cleanup failed: %s", exc, extra={"source": "cli"})


def run_tick(client: ApiClient, *, source_repo: Path) -> int:
    """Fire one tick against ``source_repo`` using an isolated worktree.

    Clones (or fetches) ``source_repo`` into the autoclaude workspace,
    stores all runtime state under the clone (never the user's checkout),
    and runs each step inside a dedicated git worktree on its own branch.
    Returns an exit code: 0 success, nonzero on error.
    """
    try:
        workspace = Workspace.for_source(source_repo)
        workspace.sync(source_repo)
    except WorkspaceError as exc:
        _log.error("[red]workspace sync failed[/red]: %s", exc, extra={"source": "cli"})
        return EXIT_FAILED

    storage = RepoStorage.from_repo(workspace.clone_path)
    storage.ensure()
    cfg = repo_config_mod.load(workspace.clone_path)
    storage.prune(cfg.retention)
    storage.clean_tmp()

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
        return _run_tick_locked(client, workspace=workspace, storage=storage)
    finally:
        tick_lock.release()


def _run_tick_locked(  # noqa: PLR0911, PLR0915 (exit-code dispatch mirrors run_tick)
    client: ApiClient,
    *,
    workspace: Workspace,
    storage: RepoStorage,
) -> int:
    try:
        ctx = client.context()
    except ApiError as exc:
        _log.error("[red]context fetch failed[/red]: %s", exc, extra={"source": "cli"})
        return EXIT_FAILED

    plan = ctx.get("plan")
    if plan is None or not plan.get("steps"):
        _log.warning("[yellow]no active job; nothing to do[/yellow]", extra={"source": "cli"})
        return EXIT_OK

    _reconcile_tools(client, plan.get("tools") or [], storage=storage)

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
    started_at = _iso_now()
    storage.append_history({"event": "tick_open", "tick_id": state.tick_id, "resumed_from": resumed_from})
    _log.info("[green]tick #%s open[/green]", state.tick_id, extra={"source": "cli"})

    try:
        worktree = workspace.create_worktree(state.tick_id)
    except WorkspaceError as exc:
        _log.error("[red]worktree create failed[/red]: %s", exc, extra={"source": "cli"})
        state.status = STATUS_FAILED
        state.error = f"worktree_create -> {exc}"
        _persist_tick_outcome(storage, state, started_at=started_at)
        return EXIT_FAILED

    shutdown_requested: dict[str, bool] = {"value": False}

    def _handler(signum: int, _frame: object) -> None:  # noqa: ARG001
        shutdown_requested["value"] = True

    prev_int = signal.signal(signal.SIGINT, _handler)
    prev_term = signal.signal(signal.SIGTERM, _handler)

    try:
        with TickLogger(client, state.tick_id, repo_checkout=worktree.path):
            _send_heartbeat(client, state.tick_id, tokens=state.total_tokens, cost=state.total_cost, storage=storage)
            _execute_steps(client, state, steps, worktree.path, shutdown_requested, storage)

            try:
                client.close_tick(
                    state.tick_id,
                    status=state.status,
                    outcome="\n".join(state.outcomes),
                    error_log=state.error,
                    cost_usd=state.total_cost,
                    token_cost_estimate=state.total_tokens,
                )
            except ApiError as exc:
                _log.error("[red]tick close failed[/red]: %s", exc, extra={"source": "cli"})
                _persist_tick_outcome(storage, state, started_at=started_at)
                return EXIT_FAILED
    finally:
        signal.signal(signal.SIGINT, prev_int)
        signal.signal(signal.SIGTERM, prev_term)
        _cleanup_worktree(workspace, worktree)

    _persist_tick_outcome(storage, state, started_at=started_at)

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
