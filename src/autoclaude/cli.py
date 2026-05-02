"""Typer entrypoint for autoclaude-cli."""

from __future__ import annotations

import contextlib
import json as _json
import os
import shutil
import subprocess
import webbrowser
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Annotated

import typer

from autoclaude import __version__, claude_env, creds_watcher, repo_config
from autoclaude.api_client import ApiClient, ApiError
from autoclaude.config import DEFAULT_URL, Config, Profile
from autoclaude.daemon import DEFAULT_INTERVAL_SECONDS as DAEMON_DEFAULT_INTERVAL
from autoclaude.daemon import run_daemon
from autoclaude.gh import is_authenticated as gh_is_authenticated
from autoclaude.gh import is_installed as gh_is_installed
from autoclaude.log_uploader import replay_pending
from autoclaude.logger import get_logger, log_file_path, profile_context, streams_dir
from autoclaude.runner import (
    EXIT_ABANDONED,
    EXIT_TOKEN_EXHAUSTED,
)
from autoclaude.runner import (
    run_tick as runner_run_tick,
)
from autoclaude.scheduler import DEFAULT_INTERVAL_SECONDS as SCHEDULER_DEFAULT_INTERVAL
from autoclaude.scheduler import run_scheduler
from autoclaude.service_install import (
    ServiceInstallError,
    install_all,
    pause_scheduler,
    play_scheduler,
    restart_all,
    status_service,
    uninstall_all,
)
from autoclaude.storage import RepoStorage
from autoclaude.update_check import (
    UPGRADE_HINT,
)
from autoclaude.update_check import (
    clear_state as update_clear_state,
)
from autoclaude.update_check import (
    load_status as update_load_status,
)
from autoclaude.update_check import (
    state_path as update_state_path,
)
from autoclaude.workspace import workspace_home

# Commands that must keep working even when the persisted state says we are
# below ``min_version`` -- they are how the user diagnoses and recovers.
_BLOCKING_EXEMPT_COMMANDS = frozenset({"version", "update-check", "uninstall-services", "services"})

CLAUDE_BILLING_URL = "https://console.anthropic.com/settings/plans"

API_KEYS_PATH = "/logged-in/api"

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


def _print_version(value: bool) -> None:  # noqa: FBT001 (Typer callback signature)
    if not value:
        return
    typer.echo(__version__)
    raise typer.Exit


@app.callback()
def _main(
    ctx: typer.Context,
    profile: ProfileOption = None,
    _version: Annotated[  # noqa: FBT002 (Typer option, value is driven by the CLI flag)
        bool,
        typer.Option(
            "--version",
            "-v",
            help="Print the installed autoclaude-cli version and exit.",
            callback=_print_version,
            is_eager=True,
        ),
    ] = False,
) -> None:
    """Shared options. `--profile` works here or on any subcommand."""
    ctx.obj = {"profile": profile}
    _surface_update_status(ctx)


def _surface_update_status(ctx: typer.Context) -> None:
    """Render the daemon-recorded version notice (or block) before the command runs.

    The daemon writes ``update_check.json`` on every heartbeat; foreground
    commands read it without making their own network call. Blocking exits
    early with code 2 unless the user is running a recovery command.
    """
    status = update_load_status()
    invoked = (ctx.invoked_subcommand or "").lower()
    if status.blocking and invoked not in _BLOCKING_EXEMPT_COMMANDS:
        _log.error(
            "[red]autoclaude %s is below required minimum %s.[/red] Run: [bold]%s[/bold]",
            status.current,
            status.minimum,
            UPGRADE_HINT,
            extra={"source": "cli"},
        )
        raise typer.Exit(code=2)
    if status.outdated:
        _log.info(
            "[yellow]update available[/yellow]: %s -> %s. Run: [bold]%s[/bold]",
            status.current,
            status.latest,
            UPGRADE_HINT,
            extra={"source": "cli"},
        )


def _load(ctx: typer.Context, profile_flag: str | None) -> tuple[Config, Profile]:
    resolved = profile_flag or (ctx.obj or {}).get("profile")
    cfg = Config.load()
    return cfg, cfg.resolve(resolved)


def _select_profiles(ctx: typer.Context, profile_flag: str | None) -> list[Profile]:
    """Return the profiles a multi-profile command should run for.

    With ``--profile`` set (here or on the parent command), restrict to
    that single profile. Otherwise return every configured profile in
    sorted name order so behaviour is deterministic across runs. Missing
    config falls back to a single resolved profile so first-run users
    still get a working command.
    """
    cfg = Config.load()
    explicit = profile_flag or (ctx.obj or {}).get("profile")
    if explicit:
        return [cfg.resolve(explicit)]
    if cfg.profiles:
        return [cfg.resolve(name) for name in sorted(cfg.profiles)]
    return [cfg.resolve(None)]


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

    try:
        results = install_all()
    except ServiceInstallError as exc:
        _log.warning(
            "[yellow]service install failed[/yellow]: %s. Run `autoclaude install-services` to retry.",
            exc,
            extra={"source": "cli"},
        )
    else:
        for result in results:
            _log.info(
                "[green]service installed[/green] (%s)",
                result.detail,
                extra={"source": "cli"},
            )
        _log.info(
            "heartbeat running, scheduler ticking every %d minutes across all profiles. Use `autoclaude pause` to stop scheduled ticks.",
            int(SCHEDULER_DEFAULT_INTERVAL // 60),
            extra={"source": "cli"},
        )


@app.command(name="profiles")
def profiles_list() -> None:
    """List configured profiles."""
    cfg = Config.load()
    if not cfg.profiles:
        _log.warning("[yellow]no profiles configured[/yellow]; run `autoclaude login`.", extra={"source": "cli"})
        return
    for name, prof in sorted(cfg.profiles.items()):
        _log.info("%s -> %s", name, prof.url or "[dim]no url[/dim]", extra={"source": "cli"})


@app.command()
def diag(ctx: typer.Context, profile: ProfileOption = None) -> None:
    """Verify config, claude CLI, gh auth, repo checkout."""
    _cfg, prof = _load(ctx, profile)
    _log.info("profile: [bold]%s[/bold]", prof.name, extra={"source": "cli"})
    _log.info("url: %s", prof.url or "[red]missing[/red]", extra={"source": "cli"})
    _log.info("api_key set: %s", "yes" if prof.api_key else "[red]no[/red]", extra={"source": "cli"})
    _log.info("autoclaude_root: %s", prof.resolve_autoclaude_root(), extra={"source": "cli"})
    _log.info("workspace_home: %s", workspace_home(), extra={"source": "cli"})
    _log.info("log_file: %s", log_file_path(), extra={"source": "cli"})
    streams_path = streams_dir()
    streams_count = sum(1 for _ in streams_path.glob("claude-stream-*.log")) if streams_path.exists() else 0
    _log.info(
        "streams_dir: %s (%d archived)",
        streams_path,
        streams_count,
        extra={"source": "cli"},
    )

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

    runtime = claude_env.summarize_runtime()
    auto_mode = runtime["effective_default_mode"] == "auto"
    _log.info(
        "claude defaultMode: %s (user=%s, project=%s)",
        f"[green]{runtime['effective_default_mode']}[/green]" if auto_mode else runtime["effective_default_mode"],
        runtime["user_settings_default_mode"],
        runtime["project_settings_default_mode"],
        extra={"source": "cli"},
    )
    _log.info(
        "claude permission_mode: %s",
        runtime["claude_permission_mode"],
        extra={"source": "cli"},
    )
    if runtime["autoclaude_user_required"] and not runtime["autoclaude_user_exists"]:
        _log.warning(
            "claude runs as: [bold]%s[/bold] [yellow](autoclaude user not yet provisioned; will be created on next tick)[/yellow]",
            runtime["claude_runs_as"],
            extra={"source": "cli"},
        )
    else:
        _log.info(
            "claude runs as: [bold]%s[/bold]",
            runtime["claude_runs_as"],
            extra={"source": "cli"},
        )

    _report_creds_watcher_state(autoclaude_required=bool(runtime["autoclaude_user_required"]))

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


def _install_creds_watcher_during_init() -> None:
    """Install the credentials watcher and surface a single status line.

    Extracted from :func:`_provision_autoclaude_runtime` to keep the parent
    function's branch count below the linter threshold; both call sites
    expect the same logging shape.
    """
    try:
        watcher = creds_watcher.install_watcher()
    except creds_watcher.CredsWatcherError as exc:
        _log.warning(
            "[yellow]credentials watcher install failed[/yellow]: %s. "
            "Re-run `autoclaude init --user-autoclaude` once the cause is fixed.",
            exc,
            extra={"source": "cli"},
        )
        return
    if watcher.action == "installed":
        _log.info(
            "[green]credentials watcher installed[/green] (%s)",
            watcher.detail,
            extra={"source": "cli"},
        )
    elif watcher.action == "skipped":
        _log.info(
            "[dim]credentials watcher skipped[/dim]: %s",
            watcher.detail,
            extra={"source": "cli"},
        )


def _report_creds_watcher_state(*, autoclaude_required: bool) -> None:
    """Print the creds-watcher status line in ``diag`` output.

    The watcher only matters when claude is running under the autoclaude
    user (root + bypassPermissions on Linux). On other configurations the
    line is suppressed to avoid noise.
    """
    status = creds_watcher.watcher_status()
    if status == "unsupported":
        return
    if status == "active":
        _log.info("creds watcher: [green]active[/green]", extra={"source": "cli"})
        return
    if status == "not_installed":
        if autoclaude_required:
            _log.warning(
                "creds watcher: [yellow]not installed[/yellow] -- run `autoclaude init --user-autoclaude` to install it",
                extra={"source": "cli"},
            )
        else:
            _log.info(
                "creds watcher: [dim]not installed (not required: claude does not run as the autoclaude user)[/dim]",
                extra={"source": "cli"},
            )
        return
    _log.warning("creds watcher: [yellow]%s[/yellow]", status, extra={"source": "cli"})


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


def _service_state(kind: str) -> str:
    """Return the platform-reported status string for ``kind`` (``active``, ``inactive``, ...)."""
    try:
        return (status_service(kind).detail or "unknown").strip()
    except ServiceInstallError as exc:
        return f"error: {exc}"


def _format_relative_seconds(seconds: float) -> str:
    """Render ``seconds`` as a compact human-readable delta (e.g. ``8m12s``)."""
    total = round(seconds)
    if total <= 0:
        return "now"
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}h{minutes:02d}m"
    if minutes:
        return f"{minutes}m{secs:02d}s"
    return f"{secs}s"


def _resolve_next_tick(prof: Profile, level: str) -> tuple[str, str]:
    """Summarise when the scheduler will fire the next tick for ``prof``.

    Returns ``(color, label)``. The label answers a single operator
    question: "when will an automatic tick run next?". It folds in the
    states that prevent ticks (scheduler stopped, profile paused) so the
    line is always actionable.

    The next-tick estimate is ``last_tick.ended_at + scheduler interval``.
    The scheduler sleeps *after* a cycle returns, so this matches the real
    wake-up time within one tick duration. When no tick has been recorded
    yet we cannot know the cycle phase, so we fall back to a generic
    "pending" label rather than guess.
    """
    if prof.paused:
        return "yellow", "ticks skipped (profile paused)"
    if level not in {"running", "degraded"}:
        return "yellow", "scheduler stopped"

    storage = RepoStorage.from_autoclaude_root(prof.resolve_autoclaude_root())
    last_tick = storage.read_last_tick()
    ended_at_raw = (last_tick or {}).get("ended_at")
    if not ended_at_raw:
        return "dim", "pending (no recorded tick yet)"

    raw = str(ended_at_raw)
    parseable = raw[:-1] + "+00:00" if raw.endswith("Z") else raw
    try:
        ended_at = datetime.fromisoformat(parseable)
    except ValueError:
        return "dim", f"unknown (unparseable ended_at={ended_at_raw!r})"
    # The server is the canonical source for these stamps and writes UTC, but
    # if a naive datetime ever sneaks through, treat it as UTC rather than
    # crashing on the aware/naive subtraction below.
    if ended_at.tzinfo is None:
        ended_at = ended_at.replace(tzinfo=UTC)

    next_at_utc = ended_at + timedelta(seconds=SCHEDULER_DEFAULT_INTERVAL)
    now_utc = datetime.now(tz=UTC)
    delta = (next_at_utc - now_utc).total_seconds()
    # Display in the operator's local zone (the system tz). `.astimezone()`
    # without args converts an aware datetime to the local zone -- never UTC
    # unless the host has no tz configured.
    next_at_local = next_at_utc.astimezone().strftime("%H:%M:%S")
    if delta <= 0:
        return "yellow", f"due now (was scheduled at {next_at_local})"
    return "green", f"in {_format_relative_seconds(delta)} (at {next_at_local} local)"


def _resolve_autoclaude_status(prof: Profile) -> tuple[str, str]:
    """Summarise local autoclaude state for ``prof``.

    Returns ``(level, label)`` where ``level`` is one of:

    - ``"running"``: profile is not paused AND scheduler+heartbeat are active.
    - ``"paused"``: ticks won't fire (profile flag set, or scheduler stopped).
    - ``"degraded"``: scheduler is fine but heartbeat is down -- ticks still
      run, but the dashboard's "Active CLIs" KPI will go stale.

    The label is a short, human-readable string suitable for a single status
    line. Built from the profile's ``paused`` config flag plus the OS service
    states resolved via ``status_service``.
    """
    flags: list[str] = []
    paused = False
    degraded = False

    if prof.paused:
        flags.append("profile paused")
        paused = True

    scheduler_state = _service_state("scheduler")
    if scheduler_state != "active":
        flags.append(f"scheduler {scheduler_state}")
        paused = True

    heartbeat_state = _service_state("heartbeat")
    if heartbeat_state != "active":
        flags.append(f"heartbeat {heartbeat_state}")
        if not paused:
            degraded = True

    if not flags:
        return "running", "running (scheduler + heartbeat active)"
    if paused:
        return "paused", "paused: " + ", ".join(flags)
    if degraded:
        return "degraded", "degraded: " + ", ".join(flags)
    return "running", "running"


@app.command()
def status(ctx: typer.Context, profile: ProfileOption = None) -> None:
    """Print the next-job summary for every configured profile.

    Iterates profiles in sorted name order so a single ``autoclaude
    status`` covers local + production. Pass ``--profile X`` to restrict
    to one profile.

    For each profile, prints the local autoclaude state (``running`` /
    ``paused`` / ``degraded``) on top of the next-job summary so an operator
    can tell at a glance whether ticks will actually fire.
    """
    selected = _select_profiles(ctx, profile)
    failures = 0
    for prof in selected:
        with profile_context(prof.name):
            _log.info("url: %s", prof.url, extra={"source": "cli"})
            level, label = _resolve_autoclaude_status(prof)
            color = {"running": "green", "paused": "yellow", "degraded": "yellow"}.get(level, "yellow")
            log_fn = _log.info if level == "running" else _log.warning
            log_fn("autoclaude: [%s]%s[/%s]", color, label, color, extra={"source": "cli"})
            next_color, next_label = _resolve_next_tick(prof, level)
            _log.info("next tick: [%s]%s[/%s]", next_color, next_label, next_color, extra={"source": "cli"})
            try:
                with ApiClient(prof, cli_version=__version__) as client:
                    api_ctx = client.context()
            except ApiError as exc:
                _log.error("[red]context fetch failed[/red]: %s", exc, extra={"source": "cli"})
                failures += 1
                continue
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
    if failures and failures == len(selected):
        raise typer.Exit(code=1)


def _run_tick_for_profile(prof: Profile) -> int:
    """Run one tick for ``prof``. Returns the runner exit code (0 on success).

    Each profile owns its own ``ApiClient``, so credentials and HTTP
    sessions stay isolated even when called back-to-back. The active
    profile is set on the logger context so every line emitted during
    this call carries the profile tag.
    """
    with profile_context(prof.name):
        try:
            with ApiClient(prof, cli_version=__version__) as client:
                with contextlib.suppress(Exception):
                    replay_pending(client)
                exit_code = runner_run_tick(client)
        except ApiError as exc:
            _log.error("[red]api error[/red]: %s", exc, extra={"source": "cli"})
            return 1
        if exit_code == EXIT_TOKEN_EXHAUSTED:
            _log.error(
                "[red]Claude subscription out of tokens.[/red] Top up at %s then re-run.",
                CLAUDE_BILLING_URL,
                extra={"source": "cli"},
            )
        elif exit_code == EXIT_ABANDONED:
            _log.warning(
                "[yellow]tick abandoned; server will accept the next tick as a resume.[/yellow]",
                extra={"source": "cli"},
            )
        return exit_code


@app.command()
def tick(
    ctx: typer.Context,
    profile: ProfileOption = None,
) -> None:
    """Run one tick per configured profile, sequentially.

    Default behaviour iterates every profile in sorted name order so a
    single ``autoclaude tick`` drains queues across local + production
    in one pass. Pass ``--profile X`` to restrict to a single profile.

    Each profile clones into its own ``$AUTOCLAUDE_HOME/repos/<slug>/``
    and ticks inside a dedicated git worktree on its own branch. No
    local checkout is involved.
    """
    selected = _select_profiles(ctx, profile)
    if len(selected) > 1:
        _log.info(
            "[bold]ticking %d profiles:[/bold] %s",
            len(selected),
            ", ".join(p.name for p in selected),
            extra={"source": "cli"},
        )
    results: dict[str, int] = {}
    for prof in selected:
        results[prof.name] = _run_tick_for_profile(prof)

    if len(selected) > 1:
        for name, code in results.items():
            marker = "[green]ok[/green]" if code == 0 else f"[red]exit={code}[/red]"
            _log.info("%s -> %s", name, marker, extra={"source": "cli"})

    worst = max(results.values()) if results else 0
    if worst != 0:
        raise typer.Exit(code=worst)


@app.command()
def init(
    repo: Annotated[
        Path | None,
        typer.Option("--repo", help="Repo to scaffold. Defaults to the current directory."),
    ] = None,
    user_autoclaude: Annotated[  # noqa: FBT002 (Typer flag)
        bool,
        typer.Option(
            "--user-autoclaude",
            help=(
                "Provision the dedicated `autoclaude` system user (and adjust the necessary "
                "permissions) without prompting and without scaffolding a repo. Use this when "
                "a tick failed with `autoclaude user is not provisioned`."
            ),
        ),
    ] = False,
) -> None:
    """Scaffold ``.autoclaude/`` in the target repo (idempotent).

    Creates the folder skeleton, writes the managed ``.gitignore``, stamps
    ``META.json`` with the current schema version, and drops a default
    ``config.toml`` if none exists. Safe to re-run -- existing config files
    are never overwritten.

    Also provisions the dedicated ``autoclaude`` system user up-front when this
    host will need it (root + ``--permission-mode bypassPermissions``), so the
    first tick does not pay the cost or surprise the operator. The user is only
    created after explicit confirmation.

    Pass ``--user-autoclaude`` to skip the repo scaffolding and run *only* the
    user provisioning + permission grants, without the interactive prompt. This
    is the recovery path the runner points to when a tick fails because the
    user is missing.
    """
    if user_autoclaude:
        _provision_autoclaude_runtime(cwd=Path.cwd(), force=True, interactive=False)
        return

    root = (repo or Path.cwd()).resolve()
    storage = RepoStorage.from_repo(root)
    storage.ensure()
    config_path = repo_config.scaffold_default(root)
    _log.info("initialised [bold]%s[/bold]", storage.root, extra={"source": "cli"})
    _log.info("config: %s", config_path, extra={"source": "cli"})
    _provision_autoclaude_runtime(cwd=root, force=False, interactive=True)


def _provision_autoclaude_runtime(*, cwd: Path, force: bool, interactive: bool) -> None:
    """Provision the ``autoclaude`` system user and apply the install-time permission grants.

    Single source of truth shared by ``autoclaude init`` (interactive prompt)
    and ``autoclaude init --user-autoclaude`` (forced, non-interactive recovery
    path). After this returns successfully, every subsequent tick can wrap
    ``claude`` with ``runuser -u autoclaude`` without doing any further
    chgrp/useradd work mid-step.

    Args:
        cwd: where to look for a project-level ``.claude/settings.json`` to
            decide if ``defaultMode=auto`` is in effect.
        force: when ``True``, run the provisioning even if ``defaultMode=auto``
            would normally make it unnecessary. ``--user-autoclaude`` sets this
            -- the operator opted in explicitly.
        interactive: when ``True``, prompt before creating the user. The flag
            form skips the prompt.
    """
    if not claude_env.is_root():
        if force:
            _log.error(
                "[red]--user-autoclaude requires root[/red] to create system users",
                extra={"source": "cli"},
            )
        return
    if not force and not claude_env.should_bypass_permissions(cwd=cwd):
        # Auto mode: claude accepts root, no privilege drop needed.
        return

    if claude_env.autoclaude_user_exists():
        _log.info(
            "[dim]system user '%s' already provisioned[/dim]",
            claude_env.AUTOCLAUDE_USER,
            extra={"source": "cli"},
        )
    else:
        if interactive:
            _log.info(
                "[yellow]This host runs autoclaude as root and uses --permission-mode bypassPermissions, "
                "which `claude` refuses to combine with UID 0.[/yellow]",
                extra={"source": "cli"},
            )
            _log.info(
                "[yellow]autoclaude can create a dedicated system user '%s' (and group of the same "
                "name, with root added to it) so claude can run unprivileged.[/yellow]",
                claude_env.AUTOCLAUDE_USER,
                extra={"source": "cli"},
            )
            if not typer.confirm(f"Create system user '{claude_env.AUTOCLAUDE_USER}' now?", default=True):
                _log.warning(
                    "skipped: the first tick will fail with `autoclaude user is not provisioned` until you run `autoclaude init --user-autoclaude`.",
                    extra={"source": "cli"},
                )
                return
        try:
            claude_env.ensure_autoclaude_user()
        except claude_env.UserCreationError as exc:
            _log.error("[red]user provisioning failed[/red]: %s", exc, extra={"source": "cli"})
            return
        _log.info(
            "[green]system user '%s' provisioned[/green]",
            claude_env.AUTOCLAUDE_USER,
            extra={"source": "cli"},
        )

    # Apply install-time permission grants. Both helpers are idempotent and
    # cached per process, so this is safe to re-run on every `init`.
    try:
        claude_env.share_claude_config()
        claude_env.share_claude_binary()
        claude_env.share_gh_config()
        claude_env.share_workspace_home()
    except claude_env.UserCreationError as exc:
        _log.error("[red]permission setup failed[/red]: %s", exc, extra={"source": "cli"})
        return
    _log.info(
        "[green]claude config, gh config, binary path and workspace home permissions granted to the '%s' group[/green]",
        claude_env.AUTOCLAUDE_GROUP,
        extra={"source": "cli"},
    )

    _install_creds_watcher_during_init()


@app.command()
def version() -> None:
    """Print the package version."""
    _log.info("%s", __version__, extra={"source": "cli"})


@app.command(name="update-check")
def update_check_cmd(
    clear: Annotated[  # noqa: FBT002 (Typer option)
        bool,
        typer.Option("--clear", help="Wipe the persisted update_check.json and exit."),
    ] = False,
) -> None:
    """Inspect the daemon-recorded update status (or clear it).

    The daemon writes ``$XDG_CONFIG_HOME/autoclaude/update_check.json`` on
    every heartbeat; this command surfaces the contents so a developer can
    confirm the upgrade-notice plumbing without tailing the daemon log.
    Combine with ``AUTOCLAUDE_FORCE_LATEST=2.0.0 AUTOCLAUDE_FORCE_MIN=1.0.0``
    when running ``autoclaude daemon`` locally to rehearse the flow.
    """
    if clear:
        removed = update_clear_state()
        if removed:
            _log.info("[green]cleared[/green] %s", update_state_path(), extra={"source": "cli"})
        else:
            _log.info("[dim]no state file at %s[/dim]", update_state_path(), extra={"source": "cli"})
        return
    status = update_load_status()
    _log.info("state file: %s", update_state_path(), extra={"source": "cli"})
    _log.info(
        "current=%s latest=%s min=%s outdated=%s blocking=%s",
        status.current,
        status.latest or "[dim]unknown[/dim]",
        status.minimum or "[dim]unknown[/dim]",
        status.outdated,
        status.blocking,
        extra={"source": "cli"},
    )
    if status.state.checked_at:
        _log.info("last heartbeat at: %s", status.state.checked_at, extra={"source": "cli"})
    else:
        _log.info(
            "[dim]no heartbeat recorded yet -- start the daemon with `autoclaude daemon`[/dim]",
            extra={"source": "cli"},
        )


@app.command()
def daemon(
    ctx: typer.Context,
    profile: ProfileOption = None,
    interval: Annotated[
        float,
        typer.Option(
            "--interval",
            help="Heartbeat cadence in seconds (server may dial it down via the response).",
        ),
    ] = DAEMON_DEFAULT_INTERVAL,
) -> None:
    """Run the background heartbeat in the foreground.

    Heartbeats every configured profile sequentially per cycle. Pass
    ``--profile X`` to restrict to a single profile. Normally launched
    by the per-user service installed via ``autoclaude login`` (or
    ``autoclaude install-services``); SIGINT/SIGTERM exits cleanly.
    """
    selected = _select_profiles(ctx, profile)
    try:
        with contextlib.ExitStack() as stack:
            clients = [stack.enter_context(ApiClient(p, cli_version=__version__)) for p in selected]
            run_daemon(clients, cli_version=__version__, interval=interval)
    except ApiError as exc:
        _log.error("[red]daemon api error[/red]: %s", exc, extra={"source": "cli"})
        raise typer.Exit(code=1) from exc


@app.command()
def scheduler(
    ctx: typer.Context,
    profile: ProfileOption = None,
    interval: Annotated[
        float,
        typer.Option(
            "--interval",
            help="Tick cadence in seconds (clamped to >= 15 minutes).",
        ),
    ] = SCHEDULER_DEFAULT_INTERVAL,
) -> None:
    """Run the periodic tick scheduler in the foreground.

    Each cycle ticks every configured profile sequentially. Pass
    ``--profile X`` to restrict to a single profile. Normally launched
    by the per-user service installed via ``autoclaude login`` (or
    ``autoclaude install-services``); SIGINT/SIGTERM exits cleanly.
    """
    selected = _select_profiles(ctx, profile)
    try:
        with contextlib.ExitStack() as stack:
            clients = [stack.enter_context(ApiClient(p, cli_version=__version__)) for p in selected]
            run_scheduler(clients, interval=interval)
    except ApiError as exc:
        _log.error("[red]scheduler api error[/red]: %s", exc, extra={"source": "cli"})
        raise typer.Exit(code=1) from exc


@app.command(name="install-services")
def install_services() -> None:
    """Register heartbeat + scheduler as per-user services.

    Both services run all configured profiles sequentially per cycle, so
    no profile binding is needed at install time.
    """
    try:
        results = install_all()
    except ServiceInstallError as exc:
        _log.error("[red]install failed[/red]: %s", exc, extra={"source": "cli"})
        raise typer.Exit(code=1) from exc
    for result in results:
        _log.info("[green]installed[/green] (%s)", result.detail, extra={"source": "cli"})


_LOG_KIND_CHOICES = ("scheduler", "heartbeat", "all")


@app.command()
def logs(
    kind: Annotated[
        str,
        typer.Option(
            "--kind",
            "-k",
            help="Which log to read: scheduler, heartbeat, or all (interleaved).",
            case_sensitive=False,
        ),
    ] = "scheduler",
    lines: Annotated[
        int,
        typer.Option("--lines", "-n", help="Show the last N lines before tailing."),
    ] = 100,
    follow: Annotated[  # noqa: FBT002 (Typer flag, value is driven by the CLI option)
        bool,
        typer.Option("--follow/--no-follow", "-f", help="Tail the log (Ctrl+C to stop)."),
    ] = True,
    stream: Annotated[
        str,
        typer.Option(
            "--stream",
            "-s",
            help="Which file to tail: stdout (.out.log) or stderr (.err.log).",
            case_sensitive=False,
        ),
    ] = "stdout",
) -> None:
    """Tail the heartbeat / scheduler service logs.

    The launchd / systemd / Task Scheduler unit writes ``stdout`` and
    ``stderr`` to ``~/.config/autoclaude/logs/<kind>.{out,err}.log``.
    Defaults: scheduler stdout, last 100 lines, follow.
    """
    kind_value = kind.lower().strip()
    if kind_value not in _LOG_KIND_CHOICES:
        _log.error(
            "[red]invalid --kind %r[/red]; pick one of: %s",
            kind,
            ", ".join(_LOG_KIND_CHOICES),
            extra={"source": "cli"},
        )
        raise typer.Exit(code=1)
    stream_value = stream.lower().strip()
    if stream_value not in {"stdout", "stderr"}:
        _log.error(
            "[red]invalid --stream %r[/red]; pick one of: stdout, stderr",
            stream,
            extra={"source": "cli"},
        )
        raise typer.Exit(code=1)
    suffix = "out.log" if stream_value == "stdout" else "err.log"
    log_dir = Path.home() / ".config" / "autoclaude" / "logs"
    targets = [log_dir / f"scheduler.{suffix}", log_dir / f"heartbeat.{suffix}"] if kind_value == "all" else [log_dir / f"{kind_value}.{suffix}"]
    missing = [str(p) for p in targets if not p.exists()]
    if missing:
        _log.warning(
            "[yellow]log file(s) missing[/yellow]: %s. The service may not have run yet.",
            ", ".join(missing),
            extra={"source": "cli"},
        )
        existing = [p for p in targets if p.exists()]
        if not existing:
            raise typer.Exit(code=1)
        targets = existing

    tail_bin = shutil.which("tail")
    if tail_bin is None:
        _log.error("[red]`tail` not found on PATH[/red].", extra={"source": "cli"})
        raise typer.Exit(code=1)
    cmd = [tail_bin, "-n", str(max(0, lines))]
    if follow:
        cmd.append("-F")
    cmd.extend(str(p) for p in targets)
    with contextlib.suppress(KeyboardInterrupt):
        subprocess.run(cmd, check=False)


@app.command()
def restart() -> None:
    """Restart heartbeat + scheduler so freshly-installed code is picked up.

    Long-running services hold the old code in memory until bounced. After
    an ``uv tool install`` / ``uv tool upgrade`` (or any local rebuild), run
    this to re-apply the unit/plist template and restart both processes.
    Each service is restarted independently: a failure on one does not
    skip the other.
    """
    try:
        results = restart_all()
    except ServiceInstallError as exc:
        _log.error("[red]restart failed[/red]: %s", exc, extra={"source": "cli"})
        raise typer.Exit(code=1) from exc
    if not results:
        _log.warning(
            "[yellow]no services restarted[/yellow]; run `autoclaude install-services` first.",
            extra={"source": "cli"},
        )
        return
    for result in results:
        _log.info("[green]restarted[/green] (%s)", result.detail, extra={"source": "cli"})


@app.command(name="uninstall-services")
def uninstall_services() -> None:
    """Remove heartbeat + scheduler services and the credentials watcher."""
    try:
        results = uninstall_all()
    except ServiceInstallError as exc:
        _log.error("[red]uninstall failed[/red]: %s", exc, extra={"source": "cli"})
        raise typer.Exit(code=1) from exc
    for result in results:
        _log.info("[green]removed[/green] (%s)", result.detail, extra={"source": "cli"})
    try:
        watcher = creds_watcher.uninstall_watcher()
    except creds_watcher.CredsWatcherError as exc:
        _log.warning(
            "[yellow]credentials watcher uninstall failed[/yellow]: %s",
            exc,
            extra={"source": "cli"},
        )
    else:
        if watcher.action == "uninstalled":
            _log.info(
                "[green]credentials watcher removed[/green] (%s)",
                watcher.detail,
                extra={"source": "cli"},
            )


@app.command()
def pause(ctx: typer.Context, profile: ProfileOption = None) -> None:
    """Stop the scheduler (heartbeat keeps running).

    With ``--profile X`` (or ``AUTOCLAUDE_PROFILE``) only that profile is
    paused: the scheduler service keeps running but skips ticks for the
    paused profile until ``autoclaude --profile X play`` clears the flag.
    Without a profile flag the launchd/systemd scheduler service is
    stopped entirely (legacy behavior).
    """
    explicit = profile or (ctx.obj or {}).get("profile") or os.environ.get("AUTOCLAUDE_PROFILE")
    if explicit:
        _set_profile_paused(explicit, paused=True)
        _log.info(
            "[yellow]profile %r paused[/yellow]; scheduler will skip its ticks. Resume with `autoclaude --profile %s play`.",
            explicit,
            explicit,
            extra={"source": "cli"},
        )
        return
    try:
        result = pause_scheduler()
    except ServiceInstallError as exc:
        _log.error("[red]pause failed[/red]: %s", exc, extra={"source": "cli"})
        raise typer.Exit(code=1) from exc
    _log.info(
        "[yellow]scheduler paused[/yellow] (%s). Heartbeat untouched. Resume with `autoclaude play`.",
        result.detail,
        extra={"source": "cli"},
    )


@app.command()
def play(ctx: typer.Context, profile: ProfileOption = None) -> None:
    """Resume the scheduler.

    With ``--profile X`` only that profile's pause flag is cleared; the
    scheduler service is left untouched. Without a profile flag the
    scheduler service is started (legacy behavior).
    """
    explicit = profile or (ctx.obj or {}).get("profile") or os.environ.get("AUTOCLAUDE_PROFILE")
    if explicit:
        _set_profile_paused(explicit, paused=False)
        _log.info("[green]profile %r resumed[/green]", explicit, extra={"source": "cli"})
        return
    try:
        result = play_scheduler()
    except ServiceInstallError as exc:
        _log.error("[red]play failed[/red]: %s", exc, extra={"source": "cli"})
        raise typer.Exit(code=1) from exc
    _log.info("[green]scheduler running[/green] (%s)", result.detail, extra={"source": "cli"})


def _set_profile_paused(name: str, *, paused: bool) -> None:
    cfg = Config.load()
    profile_obj = cfg.profiles.get(name)
    if profile_obj is None:
        _log.error("[red]profile %r not found[/red]; configured: %s", name, sorted(cfg.profiles), extra={"source": "cli"})
        raise typer.Exit(code=1)
    profile_obj.paused = paused
    cfg.save()


task_app = typer.Typer(
    add_completion=False,
    help="Create user-actionable Tasks from inside an agent run.",
)
app.add_typer(task_app, name="task")


@task_app.command("create")
def task_create(
    ctx: typer.Context,
    profile: ProfileOption = None,
    kind: Annotated[str, typer.Option("--kind", help="Task kind slug (e.g. issuer_review_comment).")] = "",
    title: Annotated[str, typer.Option("--title", help="One-line summary shown in the dashboard.")] = "",
    body: Annotated[str, typer.Option("--body", help="Optional longer description.")] = "",
    action_url: Annotated[str, typer.Option("--action-url", help="Where the user goes to act on this task.")] = "",
    payload_json: Annotated[
        str,
        typer.Option("--payload-json", help="Optional JSON payload with arbitrary context."),
    ] = "",
    source: Annotated[str, typer.Option("--source", help="Free-form source slug (e.g. issuer).")] = "",
    dedupe_key: Annotated[
        str,
        typer.Option(
            "--dedupe-key",
            help="Stable key (e.g. issuer:issue:42); a non-terminal task with the same key is refreshed in place.",
        ),
    ] = "",
    team_id: Annotated[
        int | None,
        typer.Option("--team-id", help="Team to attach the task to. Defaults to the active project's team."),
    ] = None,
    project_id: Annotated[
        int | None,
        typer.Option("--project-id", help="Project to attach the task to. Defaults to the active project."),
    ] = None,
    is_blocking: Annotated[  # noqa: FBT002 (Typer option, value is driven by the CLI flag)
        bool,
        typer.Option(
            "--is-blocking/--no-is-blocking",
            help="Mark this task as blocking: the parent Job is skipped in the round-robin until the task is resolved.",
        ),
    ] = False,
) -> None:
    """POST a user-actionable Task to the AutoClaude server.

    Reads the active profile for credentials and resolves ``team_id`` /
    ``project_id`` from ``/api/ac/runner/context/`` when not supplied.
    """
    if not kind:
        msg = "--kind is required."
        raise typer.BadParameter(msg)
    if not title:
        msg = "--title is required."
        raise typer.BadParameter(msg)

    payload: dict | None = None
    if payload_json:
        try:
            payload = _json.loads(payload_json)
        except _json.JSONDecodeError as exc:
            msg = f"--payload-json must be valid JSON: {exc}"
            raise typer.BadParameter(msg) from exc
        if not isinstance(payload, dict):
            msg = "--payload-json must decode to a JSON object."
            raise typer.BadParameter(msg)

    _cfg, prof = _load(ctx, profile)
    try:
        with ApiClient(prof, cli_version=__version__) as client:
            resolved_team_id = team_id
            resolved_project_id = project_id
            if resolved_team_id is None or resolved_project_id is None:
                api_ctx = client.context()
                if resolved_team_id is None:
                    team = api_ctx.get("team") or {}
                    if team.get("id") is None:
                        _log.error(
                            "[red]no team_id resolved[/red]: pass --team-id or set up a project first.",
                            extra={"source": "cli"},
                        )
                        raise typer.Exit(code=1)
                    resolved_team_id = int(team["id"])
                if resolved_project_id is None:
                    project = api_ctx.get("project") or {}
                    if project.get("id") is not None:
                        resolved_project_id = int(project["id"])

            response = client.create_task(
                team_id=resolved_team_id,
                kind=kind,
                title=title,
                body=body,
                action_url=action_url,
                payload=payload,
                source=source,
                dedupe_key=dedupe_key,
                project_id=resolved_project_id,
                is_blocking=is_blocking,
            )
    except ApiError as exc:
        _log.error("[red]task create failed[/red]: %s", exc, extra={"source": "cli"})
        if exc.payload:
            _log.info("response: %s", exc.payload, extra={"source": "cli"})
        raise typer.Exit(code=1) from exc

    task_id = response.get("id")
    _log.info(
        "[green]task created[/green] id=%s status=%s",
        task_id,
        response.get("status"),
        extra={"source": "cli"},
    )
    typer.echo(str(task_id))


@app.command()
def services(ctx: typer.Context, profile: ProfileOption = None) -> None:
    """Print platform service status for heartbeat, scheduler, and creds watcher."""
    _load(ctx, profile)
    try:
        heartbeat = status_service("heartbeat")
        sched = status_service("scheduler")
    except ServiceInstallError as exc:
        _log.error("[red]status failed[/red]: %s", exc, extra={"source": "cli"})
        raise typer.Exit(code=1) from exc
    _log.info("heartbeat (%s): %s", heartbeat.platform, heartbeat.detail, extra={"source": "cli"})
    _log.info("scheduler (%s): %s", sched.platform, sched.detail, extra={"source": "cli"})
    watcher_state = creds_watcher.watcher_status()
    if watcher_state != "unsupported":
        _log.info("creds watcher: %s", watcher_state, extra={"source": "cli"})


if __name__ == "__main__":
    app()
