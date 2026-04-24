"""Typer entrypoint for autoclaude-cli."""

from __future__ import annotations

import contextlib
import shutil
import webbrowser
from pathlib import Path
from typing import Annotated

import typer

from autoclaude import __version__, repo_config
from autoclaude.api_client import ApiClient, ApiError
from autoclaude.config import DEFAULT_URL, Config, Profile
from autoclaude.gh import is_authenticated as gh_is_authenticated
from autoclaude.gh import is_installed as gh_is_installed
from autoclaude.log_uploader import replay_pending
from autoclaude.logger import get_logger
from autoclaude.runner import (
    EXIT_ABANDONED,
    EXIT_TOKEN_EXHAUSTED,
)
from autoclaude.runner import (
    run_tick as runner_run_tick,
)
from autoclaude.storage import RepoStorage
from autoclaude.workspace import workspace_home

CLAUDE_BILLING_URL = "https://console.anthropic.com/settings/plans"

API_KEYS_PATH = "/logged-in/settings/api-keys"

app = typer.Typer(add_completion=False, help="Local runner for AutoClaude.")
_log = get_logger("cli")

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
    _log.info("API-key page: [bold]%s[/bold]", settings_url, extra={"source": "cli"})
    if typer.confirm("Open it in your browser now?", default=True):
        with contextlib.suppress(Exception):
            webbrowser.open(settings_url)
    raw_key = typer.prompt("Paste your API key", hide_input=True).strip()
    prof.api_key = raw_key
    cfg.profiles[prof.name] = prof
    cfg.active = prof.name
    cfg.save()
    _log.info("[green]saved profile %r[/green] -> %s", prof.name, prof.url, extra={"source": "cli"})


@app.command()
def diag(ctx: typer.Context, profile: ProfileOption = None) -> None:
    """Verify config, claude CLI, gh auth, repo checkout."""
    _cfg, prof = _load(ctx, profile)
    _log.info("profile: [bold]%s[/bold]", prof.name, extra={"source": "cli"})
    _log.info("url: %s", prof.url or "[red]missing[/red]", extra={"source": "cli"})
    _log.info("api_key set: %s", "yes" if prof.api_key else "[red]no[/red]", extra={"source": "cli"})
    _log.info("repo_checkout: %s", prof.repo_checkout or "[yellow]unset[/yellow]", extra={"source": "cli"})
    _log.info("autoclaude_root: %s", prof.resolve_autoclaude_root(), extra={"source": "cli"})
    _log.info("workspace_home: %s", workspace_home(), extra={"source": "cli"})

    for binary in ("claude", "gh", "git"):
        path = shutil.which(binary)
        _log.info("%s: %s", binary, path or "[red]not found[/red]", extra={"source": "cli"})

    if not gh_is_installed():
        _log.error(
            "[red]gh CLI missing[/red]: install it from https://cli.github.com so git operations can authenticate.",
            extra={"source": "cli"},
        )
    else:
        _log.info(
            "gh auth: %s",
            "ok" if gh_is_authenticated() else "[red]NOT LOGGED IN[/red]",
            extra={"source": "cli"},
        )

    try:
        with ApiClient(prof, cli_version=__version__) as client:
            api_ctx = client.context()
            _report_protocol_state(client)
    except ApiError as exc:
        _log.error("[red]context fetch failed[/red]: %s", exc, extra={"source": "cli"})
        if exc.docs:
            _log.info(
                "[dim]attached docs (stage=%s, source=%s):[/dim]\n%s",
                exc.stage,
                exc.docs_source,
                exc.docs,
                extra={"source": "cli"},
            )
        return
    owner = api_ctx.get("owner") or {}
    _log.info(
        "resolved owner: %s",
        owner.get("email") or owner.get("username") or "?",
        extra={"source": "cli"},
    )


def _report_protocol_state(client: ApiClient) -> None:
    storage = RepoStorage.from_autoclaude_root(client.autoclaude_root)
    docs_dir = storage.api_docs_dir
    docs_count = sum(1 for _ in docs_dir.rglob("*.md")) if docs_dir.exists() else 0
    reports_dir = storage.reports_dir
    last_report = None
    if reports_dir.exists():
        candidates = sorted(reports_dir.glob("*.json"))
        if candidates:
            last_report = candidates[-1].name
    _log.info("cached docs: %d", docs_count, extra={"source": "cli"})
    _log.info("last report: %s", last_report or "[dim]none[/dim]", extra={"source": "cli"})
    last_tick = storage.read_last_tick()
    if last_tick:
        _log.info(
            "last tick: #%s status=%s cost=$%.4f",
            last_tick.get("tick_id"),
            last_tick.get("status"),
            float(last_tick.get("cost_usd") or 0.0),
            extra={"source": "cli"},
        )
    stages = client.tracker_snapshot()
    if stages:
        _log.info("protocol stages:", extra={"source": "cli"})
        for key, stage in sorted(stages.items()):
            _log.info("  %s -> %s", key, stage, extra={"source": "cli"})


@app.command()
def status(ctx: typer.Context, profile: ProfileOption = None) -> None:
    """Print the active profile and the last tick summary."""
    _cfg, prof = _load(ctx, profile)
    _log.info("profile: %s", prof.name, extra={"source": "cli"})
    _log.info("url: %s", prof.url, extra={"source": "cli"})
    try:
        with ApiClient(prof, cli_version=__version__) as client:
            api_ctx = client.context()
    except ApiError as exc:
        _log.error("[red]context fetch failed[/red]: %s", exc, extra={"source": "cli"})
        raise typer.Exit(code=1) from exc
    plan = api_ctx.get("plan")
    if plan:
        _log.info(
            "next job: #%s (%s steps, mode=%s)",
            plan.get("job_id"),
            len(plan.get("steps") or []),
            plan.get("mode"),
            extra={"source": "cli"},
        )
    else:
        _log.warning("[yellow]no active plan[/yellow]", extra={"source": "cli"})


@app.command()
def tick(
    ctx: typer.Context,
    profile: ProfileOption = None,
    repo: Annotated[
        Path | None,
        typer.Option("--repo", help="Source repo to mirror into the autoclaude workspace. Defaults to CWD or the profile's saved checkout."),
    ] = None,
) -> None:
    """Run one tick.

    The source repo is cloned into ``$AUTOCLAUDE_HOME/repos/<slug>/`` and
    each tick runs inside a dedicated git worktree on its own branch. The
    user's checkout is never modified.
    """
    cfg, prof = _load(ctx, profile)
    source = repo or (Path(prof.repo_checkout) if prof.repo_checkout else Path.cwd())
    if not (source / ".git").exists():
        _log.error("[red]not a git repo[/red]: %s", source, extra={"source": "cli"})
        raise typer.Exit(code=2)
    if repo is not None:
        prof.repo_checkout = str(repo)
        cfg.profiles[prof.name] = prof
        cfg.save()
    try:
        with ApiClient(prof, cli_version=__version__) as client:
            with contextlib.suppress(Exception):
                replay_pending(client)
            exit_code = runner_run_tick(client, source_repo=source)
    except ApiError as exc:
        _log.error("[red]api error[/red]: %s", exc, extra={"source": "cli"})
        raise typer.Exit(code=1) from exc
    if exit_code == EXIT_TOKEN_EXHAUSTED:
        _log.error(
            "[red]Claude subscription out of tokens.[/red] Top up at %s then re-run `autoclaude tick`. "
            "This tick was not counted against your hourly limit.",
            CLAUDE_BILLING_URL,
            extra={"source": "cli"},
        )
    elif exit_code == EXIT_ABANDONED:
        _log.warning(
            "[yellow]tick abandoned via shutdown signal; server will accept the next tick as a resume.[/yellow]",
            extra={"source": "cli"},
        )
    if exit_code != 0:
        raise typer.Exit(code=exit_code)


@app.command()
def init(
    repo: Annotated[
        Path | None,
        typer.Option("--repo", help="Repo to scaffold. Defaults to the current directory."),
    ] = None,
) -> None:
    """Scaffold ``.autoclaude/`` in the target repo (idempotent).

    Creates the folder skeleton, writes the managed ``.gitignore``, stamps
    ``META.json`` with the current schema version, and drops a default
    ``config.toml`` if none exists. Safe to re-run -- existing config files
    are never overwritten.
    """
    root = (repo or Path.cwd()).resolve()
    storage = RepoStorage.from_repo(root)
    storage.ensure()
    config_path = repo_config.scaffold_default(root)
    _log.info("initialised [bold]%s[/bold]", storage.root, extra={"source": "cli"})
    _log.info("config: %s", config_path, extra={"source": "cli"})


@app.command()
def version() -> None:
    """Print the package version."""
    _log.info("%s", __version__, extra={"source": "cli"})


if __name__ == "__main__":
    app()
