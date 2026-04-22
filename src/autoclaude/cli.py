"""Typer entrypoint for autoclaude-cli."""

from __future__ import annotations

import contextlib
import shutil
import subprocess
import webbrowser
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console

from autoclaude import __version__
from autoclaude.api_client import ApiClient, ApiError
from autoclaude.config import DEFAULT_URL, Config, Profile
from autoclaude.plugins import ensure_installed, list_installed
from autoclaude.runner import run_tick as runner_run_tick

API_KEYS_PATH = "/logged-in/settings/api-keys"

app = typer.Typer(add_completion=False, help="Local runner for AutoClaude.")
console = Console()

ProfileOption = Annotated[
    str | None,
    typer.Option("--profile", "-p", help="Named profile to use. Defaults to $AUTOCLAUDE_PROFILE or 'default'."),
]


def _normalize_url(raw: str) -> str:
    """Add a scheme if the user left it off and strip trailing slashes."""
    value = raw.strip().rstrip("/")
    if not value:
        return value
    if "://" not in value:
        value = ("http://" if value.startswith(("localhost", "127.")) else "https://") + value
    return value


@app.callback()
def _main(ctx: typer.Context, profile: ProfileOption = None) -> None:
    """Shared options. `--profile` works here or on any subcommand."""
    ctx.obj = {"profile": profile}


def _load(ctx: typer.Context, profile_flag: str | None) -> tuple[Config, Profile]:
    resolved = profile_flag or (ctx.obj or {}).get("profile")
    cfg = Config.load()
    return cfg, cfg.resolve(resolved)


@app.command()
def login(
    ctx: typer.Context,
    profile: ProfileOption = None,
    url: Annotated[
        str | None,
        typer.Option("--url", help=f"Override the base URL for this profile (default: {DEFAULT_URL})."),
    ] = None,
) -> None:
    """Interactive: save URL and API key for the chosen profile."""
    cfg, prof = _load(ctx, profile)
    if url:
        prof.url = _normalize_url(url)

    settings_url = f"{prof.url}{API_KEYS_PATH}"
    console.print(f"API-key page: [bold]{settings_url}[/bold]")
    if typer.confirm("Open it in your browser now?", default=True):
        with contextlib.suppress(Exception):
            webbrowser.open(settings_url)
    raw_key = typer.prompt("Paste your API key", hide_input=True).strip()
    prof.api_key = raw_key
    cfg.profiles[prof.name] = prof
    cfg.active = prof.name
    cfg.save()
    console.print(f"[green]saved profile {prof.name!r}[/green] -> {prof.url}")


@app.command()
def diag(ctx: typer.Context, profile: ProfileOption = None) -> None:
    """Verify config, claude CLI, gh auth, repo checkout."""
    _cfg, prof = _load(ctx, profile)
    console.print(f"profile: [bold]{prof.name}[/bold]")
    console.print(f"url: {prof.url or '[red]missing[/red]'}")
    console.print(f"api_key set: {'yes' if prof.api_key else '[red]no[/red]'}")
    console.print(f"repo_checkout: {prof.repo_checkout or '[yellow]unset[/yellow]'}")

    for binary in ("claude", "gh", "git"):
        path = shutil.which(binary)
        console.print(f"{binary}: {path or '[red]not found[/red]'}")

    gh_status = subprocess.run(["gh", "auth", "status"], capture_output=True, text=True, check=False)
    console.print(f"gh auth: {'ok' if gh_status.returncode == 0 else '[red]NOT LOGGED IN[/red]'}")

    try:
        with ApiClient(prof) as client:
            api_ctx = client.context()
    except ApiError as exc:
        console.print(f"[red]context fetch failed[/red]: {exc}")
        return
    owner = api_ctx.get("owner") or {}
    console.print(f"resolved owner: {owner.get('email') or owner.get('username') or '?'}")

    plugins = list_installed()
    console.print(f"claude plugins: {len(plugins)} installed")


@app.command("skills-install", hidden=False)
def skills_install(ctx: typer.Context, profile: ProfileOption = None) -> None:
    """Install all Claude Code plugins the current plan requires."""
    _cfg, prof = _load(ctx, profile)
    try:
        with ApiClient(prof) as client:
            refs = client.plugin_refs()
    except ApiError as exc:
        console.print(f"[red]plugin_refs fetch failed[/red]: {exc}")
        raise typer.Exit(code=1) from exc
    if not refs:
        console.print("[dim]no plugins required[/dim]")
        return
    try:
        installed = ensure_installed(refs)
    except RuntimeError as exc:
        console.print(f"[red]plugin install failed[/red]: {exc}")
        raise typer.Exit(code=1) from exc
    if installed:
        console.print(f"[green]installed:[/green] {', '.join(installed)}")
    else:
        console.print("[dim]all required plugins already installed[/dim]")


@app.command()
def status(ctx: typer.Context, profile: ProfileOption = None) -> None:
    """Print the active profile and the last tick summary."""
    _cfg, prof = _load(ctx, profile)
    console.print(f"profile: {prof.name}")
    console.print(f"url: {prof.url}")
    try:
        with ApiClient(prof) as client:
            api_ctx = client.context()
    except ApiError as exc:
        console.print(f"[red]context fetch failed[/red]: {exc}")
        raise typer.Exit(code=1) from exc
    plan = api_ctx.get("plan")
    if plan:
        console.print(f"next job: {plan.get('job_slug')} ({len(plan.get('steps') or [])} steps, mode={plan.get('mode')})")
    else:
        console.print("[yellow]no active plan[/yellow]")


@app.command()
def tick(
    ctx: typer.Context,
    profile: ProfileOption = None,
    repo: Annotated[
        Path | None,
        typer.Option("--repo", help="Repo checkout to run agents in. Defaults to CWD or the profile's saved checkout."),
    ] = None,
) -> None:
    """Run one tick."""
    cfg, prof = _load(ctx, profile)
    checkout = repo or (Path(prof.repo_checkout) if prof.repo_checkout else Path.cwd())
    if not (checkout / ".git").exists():
        console.print(f"[red]not a git repo[/red]: {checkout}")
        raise typer.Exit(code=2)
    if repo is not None:
        prof.repo_checkout = str(repo)
        cfg.profiles[prof.name] = prof
        cfg.save()
    try:
        with ApiClient(prof) as client:
            exit_code = runner_run_tick(client, repo_checkout=checkout)
    except ApiError as exc:
        console.print(f"[red]api error[/red]: {exc}")
        raise typer.Exit(code=1) from exc
    if exit_code != 0:
        raise typer.Exit(code=exit_code)


@app.command()
def version() -> None:
    """Print the package version."""
    console.print(__version__)


if __name__ == "__main__":
    app()
