"""Orchestrate one tick: fetch context, execute steps, close."""

from __future__ import annotations

from pathlib import Path

from rich.console import Console

from autoclaude import __version__
from autoclaude.api_client import ApiClient, ApiError
from autoclaude.claude_proc import run_step
from autoclaude.plugins import ensure_installed

console = Console()


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

    required_plugins = set()
    for step in plan["steps"]:
        required_plugins.update(step.get("plugin_refs") or [])
    if required_plugins:
        try:
            installed = ensure_installed(sorted(required_plugins))
            if installed:
                console.print(f"[dim]installed plugins:[/dim] {', '.join(installed)}")
        except RuntimeError as exc:
            console.print(f"[red]plugin install failed[/red]: {exc}")
            return 1

    try:
        tick = client.open_tick(runner_version=__version__)
    except ApiError as exc:
        console.print(f"[red]tick open failed[/red]: {exc}")
        return 1

    tick_id = tick["id"]
    console.print(f"[green]tick #{tick_id} open[/green]")

    total_cost = 0.0
    final_status = "succeeded"
    final_error = ""
    outcome_lines: list[str] = []

    for ordinal, step in enumerate(plan["steps"]):
        agent = step["agent_slug"]
        try:
            opened = client.open_step(tick_id=tick_id, agent_slug=agent, ordinal=ordinal, name=agent)
        except ApiError as exc:
            final_status = "failed"
            final_error = f"step_open {agent} -> {exc}"
            break
        console.print(f"[cyan]→[/cyan] {agent}")
        result = run_step(step["prompt"], cwd=repo_checkout)
        total_cost += result.total_cost_usd
        summary = result.stdout[-1000:] if result.ok else ""
        error_log = "" if result.ok else (result.stderr or result.stdout)[-2000:]
        try:
            client.close_step(opened["id"], summary=summary, error_log=error_log)
        except ApiError as exc:
            final_status = "failed"
            final_error = f"step_close {agent} -> {exc}"
            break
        if not result.ok:
            final_status = "failed"
            final_error = error_log
            break
        outcome_lines.append(f"{agent}: ok")

    try:
        client.close_tick(
            tick_id,
            status=final_status,
            outcome="\n".join(outcome_lines),
            error_log=final_error,
            cost_usd=total_cost,
        )
    except ApiError as exc:
        console.print(f"[red]tick close failed[/red]: {exc}")
        return 1

    console.print(
        f"[green]tick #{tick_id} closed[/green] status={final_status} cost=${total_cost:.4f}",
    )
    return 0 if final_status == "succeeded" else 1
