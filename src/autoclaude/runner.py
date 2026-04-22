"""Orchestrate one tick: fetch context, execute steps, close."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from rich.console import Console

from autoclaude import __version__
from autoclaude.api_client import ApiClient, ApiError
from autoclaude.claude_proc import run_step
from autoclaude.plugins import ensure_installed

console = Console()

_SUMMARY_CHARS = 1000
_ERROR_CHARS = 2000


@dataclass
class _TickState:
    tick_id: int
    total_cost: float = 0.0
    status: str = "succeeded"
    error: str = ""
    outcomes: list[str] = field(default_factory=list)


def _install_plugins(steps: list[dict[str, Any]]) -> bool:
    refs: set[str] = set()
    for step in steps:
        refs.update(step.get("plugin_refs") or [])
    if not refs:
        return True
    try:
        installed = ensure_installed(sorted(refs))
    except RuntimeError as exc:
        console.print(f"[red]plugin install failed[/red]: {exc}")
        return False
    if installed:
        console.print(f"[dim]installed plugins:[/dim] {', '.join(installed)}")
    return True


def _execute_steps(client: ApiClient, state: _TickState, steps: list[dict[str, Any]], repo_checkout: Path) -> None:
    for ordinal, step in enumerate(steps):
        agent = step["agent_slug"]
        try:
            opened = client.open_step(tick_id=state.tick_id, agent_slug=agent, ordinal=ordinal, name=agent)
        except ApiError as exc:
            state.status = "failed"
            state.error = f"step_open {agent} -> {exc}"
            return
        console.print(f"[cyan]→[/cyan] {agent}")
        result = run_step(step["prompt"], cwd=repo_checkout)
        state.total_cost += result.total_cost_usd
        summary = result.stdout[-_SUMMARY_CHARS:] if result.ok else ""
        error_log = "" if result.ok else (result.stderr or result.stdout)[-_ERROR_CHARS:]
        try:
            client.close_step(opened["id"], summary=summary, error_log=error_log)
        except ApiError as exc:
            state.status = "failed"
            state.error = f"step_close {agent} -> {exc}"
            return
        if not result.ok:
            state.status = "failed"
            state.error = error_log
            return
        state.outcomes.append(f"{agent}: ok")


def run_tick(client: ApiClient, *, repo_checkout: Path) -> int:
    """Fire one tick. Returns an exit code: 0 success, nonzero on error."""
    try:
        ctx = client.context()
    except ApiError as exc:
        console.print(f"[red]context fetch failed[/red]: {exc}")
        return 1

    plan = ctx.get("plan")
    if plan is None or not plan.get("steps"):
        console.print("[yellow]no active job; nothing to do[/yellow]")
        return 0

    steps = plan["steps"]
    if not _install_plugins(steps):
        return 1

    try:
        tick = client.open_tick(runner_version=__version__)
    except ApiError as exc:
        console.print(f"[red]tick open failed[/red]: {exc}")
        return 1

    state = _TickState(tick_id=tick["id"])
    console.print(f"[green]tick #{state.tick_id} open[/green]")

    _execute_steps(client, state, steps, repo_checkout)

    try:
        client.close_tick(
            state.tick_id,
            status=state.status,
            outcome="\n".join(state.outcomes),
            error_log=state.error,
            cost_usd=state.total_cost,
        )
    except ApiError as exc:
        console.print(f"[red]tick close failed[/red]: {exc}")
        return 1

    console.print(f"[green]tick #{state.tick_id} closed[/green] status={state.status} cost=${state.total_cost:.4f}")
    return 0 if state.status == "succeeded" else 1
