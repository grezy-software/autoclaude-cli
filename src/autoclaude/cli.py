"""Typer entrypoint for autoclaude-cli."""

from __future__ import annotations

import contextlib
import json as _json
import shutil
import subprocess
import webbrowser
from pathlib import Path
from typing import Annotated

import typer

from autoclaude import __version__, claude_env, repo_config
from autoclaude.api_client import ApiClient, ApiError
from autoclaude.config import DEFAULT_URL, Config, Profile
from autoclaude.daemon import DEFAULT_INTERVAL_SECONDS as DAEMON_DEFAULT_INTERVAL
from autoclaude.daemon import run_daemon
from autoclaude.gh import is_authenticated as gh_is_authenticated
from autoclaude.gh import is_installed as gh_is_installed
from autoclaude.log_uploader import replay_pending
from autoclaude.logger import get_logger, profile_context
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
            "heartbeat running, scheduler ticking every %d minutes across all profiles. "
            "Use `autoclaude pause` to stop scheduled ticks.",
            int(SCHEDULER_DEFAULT_INTERVAL // 60),
            extra={"source": "cli"},
        )


@app.command(name="profiles")
def profiles_list() -> None:
    """List configured profiles, marking the active one."""
    cfg = Config.load()
    if not cfg.profiles:
        _log.warning("[yellow]no profiles configured[/yellow]; run `autoclaude login`.", extra={"source": "cli"})
        return
    for name, prof in sorted(cfg.profiles.items()):
        marker = "*" if name == cfg.active else " "
        _log.info("%s %s -> %s", marker, name, prof.url or "[dim]no url[/dim]", extra={"source": "cli"})


@app.command()
def diag(ctx: typer.Context, profile: ProfileOption = None) -> None:
    """Verify config, claude CLI, gh auth, repo checkout."""
    _cfg, prof = _load(ctx, profile)
    _log.info("profile: [bold]%s[/bold]", prof.name, extra={"source": "cli"})
    _log.info("url: %s", prof.url or "[red]missing[/red]", extra={"source": "cli"})
    _log.info("api_key set: %s", "yes" if prof.api_key else "[red]no[/red]", extra={"source": "cli"})
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
    _log.info(
        "claude runs as: [bold]%s[/bold]",
        runtime["claude_runs_as"],
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
    """Print the next-job summary for every configured profile.

    Iterates profiles in sorted name order so a single ``autoclaude
    status`` covers local + production. Pass ``--profile X`` to restrict
    to one profile.
    """
    selected = _select_profiles(ctx, profile)
    failures = 0
    for prof in selected:
        with profile_context(prof.name):
            _log.info("url: %s", prof.url, extra={"source": "cli"})
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
    targets = (
        [log_dir / f"scheduler.{suffix}", log_dir / f"heartbeat.{suffix}"]
        if kind_value == "all"
        else [log_dir / f"{kind_value}.{suffix}"]
    )
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


@app.command(name="uninstall-services")
def uninstall_services() -> None:
    """Remove heartbeat + scheduler services."""
    try:
        results = uninstall_all()
    except ServiceInstallError as exc:
        _log.error("[red]uninstall failed[/red]: %s", exc, extra={"source": "cli"})
        raise typer.Exit(code=1) from exc
    for result in results:
        _log.info("[green]removed[/green] (%s)", result.detail, extra={"source": "cli"})


@app.command()
def pause() -> None:
    """Stop the scheduler (heartbeat keeps running)."""
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
def play() -> None:
    """Resume the scheduler."""
    try:
        result = play_scheduler()
    except ServiceInstallError as exc:
        _log.error("[red]play failed[/red]: %s", exc, extra={"source": "cli"})
        raise typer.Exit(code=1) from exc
    _log.info("[green]scheduler running[/green] (%s)", result.detail, extra={"source": "cli"})


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
    """Print platform service status for heartbeat + scheduler."""
    _load(ctx, profile)
    try:
        heartbeat = status_service("heartbeat")
        sched = status_service("scheduler")
    except ServiceInstallError as exc:
        _log.error("[red]status failed[/red]: %s", exc, extra={"source": "cli"})
        raise typer.Exit(code=1) from exc
    _log.info("heartbeat (%s): %s", heartbeat.platform, heartbeat.detail, extra={"source": "cli"})
    _log.info("scheduler (%s): %s", sched.platform, sched.detail, extra={"source": "cli"})


if __name__ == "__main__":
    app()
