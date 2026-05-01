"""Invoke the ``claude`` CLI for one agent step and stream its output.

``run_step`` spawns ``claude -p <prompt> --output-format stream-json --verbose``
via ``subprocess.Popen`` and parses each JSONL event off stdout in real time.
Per-event log records are pushed through the autoclaude logger so the backend
sees what the agent is doing while it runs (tool calls, tool results,
assistant text), not only the final JSON blob at exit.

Failure detection has three layers:

1. ``returncode != 0`` -- the claude subprocess crashed.
2. ``is_error: true`` on the final ``result`` event -- claude itself signalled
   an error (auth failure, rate limit, malformed prompt).
3. The ``[autoclaude:fail] <reason>`` marker in the ``result`` text --
   the agent chose to bail out on a task it could not complete. Agents
   are expected to emit this on its own line when they stop short.
"""

from __future__ import annotations

import contextlib
import contextvars
import json
import os
import re
import shlex
import subprocess
import threading
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import IO, Any

from autoclaude import claude_env
from autoclaude.claude_env import UserCreationError
from autoclaude.logger import allocate_stream_log_path, get_logger

_log = get_logger("claude")

# All agent steps run on Opus: agents are the product surface, and the Haiku
# side-model that claude-code otherwise spins up for routing/summarisation adds
# a failure mode (e.g. image-fetching) we cannot debug from here. Worth the cost.
_AGENT_MODEL = "opus"

# Max chars for the per-step summary shipped to the dashboard. Kept short so
# the Steps table shows a single-line takeaway rather than a wall of text.
_SHORT_SUMMARY_CHARS = 240

# Watchdog: once we observe a ``{"type":"result"}`` event on stdout, claude is
# expected to flush and exit. Bun-based builds occasionally retain active
# handles past the result (periodic timers, MCP keepalives) and park in
# ``do_epoll_wait`` indefinitely. After this many seconds we force-terminate
# rather than wait the full ``timeout`` budget. The output captured up to the
# kill is preserved; ``ok`` is computed from the result event, not the rc.
_DEFAULT_POST_RESULT_GRACE_SECS = 30.0

# Watchdog: kill if no output has been seen on either stream for this long.
# Catches hangs that occur BEFORE the result event (pre-init hang where claude
# never emits its ``system`` event, mid-stream Bun freeze, etc.). Set to 5 min:
# in ``-p stream-json --verbose`` mode claude streams an event for every
# tool_use, tool_result, and incremental assistant text block, so a 5 min total
# silence is well outside normal behavior. Long single tool calls (e.g. a slow
# pytest) emit ``tool_use`` immediately and are bounded by the tool's own
# timeout, so they do not trigger a false positive.
_DEFAULT_IDLE_TIMEOUT_SECS: float | None = 300.0

# Polling interval of the watchdog loop. The trade-off is between extra wakeups
# and the granularity at which kill_reason is detected. 0.5s is well below the
# scale of tick durations (30s..30min) and not a measurable cost.
_WATCHDOG_POLL_SECS = 0.5

# After SIGTERM, the time we wait before escalating to SIGKILL. ``runuser``
# forwards SIGTERM to its claude child, so this also covers the wrapper case.
_TERM_GRACE_SECS = 5.0

# Sentinel for ``run_step`` keyword arguments that should resolve to the
# module-level default at call time, not at function-definition time. Using
# ``None`` would conflict with ``idle_timeout=None`` which legitimately means
# "watchdog disabled". A dedicated sentinel keeps both semantics distinct and
# lets tests / callers re-tune the default by patching the module constant.
_USE_DEFAULT: Any = object()

# Convention for agents that hit an unrecoverable precondition (missing remote,
# auth failure detected mid-run, etc.). When the agent ends its response with
# this marker on its own line, the runner treats the step as failed even though
# the claude subprocess exited cleanly.
_BAIL_MARKER_RE = re.compile(r"^\s*\[autoclaude:fail\]\s*(?P<reason>.*?)\s*$", re.MULTILINE)

# Billing-type failures look like bugs to an exit-code-only checker. These
# patterns let us flag them so they are not counted as retries and the user
# gets a clear billing prompt instead of a stacktrace summary.
_TOKEN_EXHAUSTION_PATTERNS: tuple[str, ...] = (
    "credit balance is too low",
    "credit balance too low",
    "insufficient_quota",
    "exceeded your current quota",
    "usage limit reached",
    "claude ai usage limit",
    "out of credits",
    "subscription is required",
)


def detect_token_exhaustion(stdout: str, stderr: str, parsed: dict | None) -> bool:
    """Return True when the ``claude`` run signals an out-of-tokens condition.

    Heuristic: matched case-insensitively across stdout, stderr, and the common
    string fields of the parsed JSON result.
    """
    haystacks: list[str] = [stdout.lower(), stderr.lower()]
    if isinstance(parsed, dict):
        for key in ("error", "result", "message"):
            value = parsed.get(key)
            if isinstance(value, str):
                haystacks.append(value.lower())
    return any(pattern in h for pattern in _TOKEN_EXHAUSTION_PATTERNS for h in haystacks)


@dataclass
class ClaudeResult:
    ok: bool
    stdout: str
    stderr: str
    session_id: str = ""
    total_cost_usd: float = 0.0
    token_cost_estimate: int = 0
    duration_ms: int = 0
    token_exhausted: bool = False
    summary: str = ""
    fail_reason: str = ""
    events: list[dict[str, Any]] = field(default_factory=list)


def _truncate(text: str, limit: int = _SHORT_SUMMARY_CHARS) -> str:
    collapsed = " ".join((text or "").split())
    if len(collapsed) <= limit:
        return collapsed
    return collapsed[: limit - 1].rstrip() + "…"


def _stringify_content(content: Any) -> str:
    """Flatten a tool_result content array to a string for log payloads."""
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, dict):
                parts.append(item.get("text") or json.dumps(item, default=str))
            else:
                parts.append(str(item))
        return "\n".join(parts)
    if isinstance(content, str):
        return content
    if content is None:
        return ""
    return json.dumps(content, default=str)


def _tool_input_summary(name: str, inp: dict[str, Any]) -> str:  # noqa: PLR0911 (one return per known tool keeps the dispatch flat and obvious)
    if not isinstance(inp, dict):
        return ""
    if name == "Bash":
        return inp.get("command", "")
    if name in {"Read", "Edit", "Write", "NotebookEdit"}:
        return str(inp.get("file_path") or inp.get("notebook_path") or "")
    if name in {"Glob", "Grep"}:
        return inp.get("pattern", "")
    if name == "WebFetch":
        return inp.get("url", "")
    if name == "WebSearch":
        return inp.get("query", "")
    if name in {"Task", "Agent"}:
        return inp.get("description") or inp.get("prompt", "")
    try:
        return json.dumps(inp, default=str)
    except (TypeError, ValueError):
        return ""


def _log_event(event: dict[str, Any], *, step_id: int | None) -> None:
    """Translate one stream-json event into a structured log record."""
    etype = event.get("type")
    if etype == "system":
        subtype = event.get("subtype") or "init"
        _log.info(
            "claude %s session=%s model=%s",
            subtype,
            event.get("session_id") or "?",
            event.get("model") or "?",
            extra={
                "source": "claude_stdout",
                "step_id": step_id,
                "payload": {
                    "event": "system",
                    "subtype": subtype,
                    "session_id": event.get("session_id"),
                    "model": event.get("model"),
                    "cwd": event.get("cwd"),
                },
            },
        )
        return
    if etype == "assistant":
        msg = event.get("message") or {}
        for block in msg.get("content") or []:
            btype = block.get("type")
            if btype == "text":
                text = block.get("text") or ""
                _log.info(
                    "assistant: %s",
                    text,
                    extra={
                        "source": "claude_stdout",
                        "step_id": step_id,
                        "payload": {"event": "assistant_text", "text": text},
                    },
                )
            elif btype == "tool_use":
                name = block.get("name") or "?"
                summary = _tool_input_summary(name, block.get("input") or {})
                _log.info(
                    "tool_use %s%s",
                    name,
                    f": {summary}" if summary else "",
                    extra={
                        "source": "claude_stdout",
                        "step_id": step_id,
                        "payload": {
                            "event": "tool_use",
                            "tool": name,
                            "tool_use_id": block.get("id"),
                            "input": block.get("input") or {},
                        },
                    },
                )
        return
    if etype == "user":
        msg = event.get("message") or {}
        for block in msg.get("content") or []:
            if block.get("type") != "tool_result":
                continue
            is_error = bool(block.get("is_error"))
            content_str = _stringify_content(block.get("content"))
            label = "tool_result (error)" if is_error else "tool_result"
            log_fn = _log.error if is_error else _log.info
            log_fn(
                "%s: %s",
                label,
                content_str,
                extra={
                    "source": "claude_stdout",
                    "step_id": step_id,
                    "payload": {
                        "event": "tool_result",
                        "tool_use_id": block.get("tool_use_id"),
                        "is_error": is_error,
                        "content": content_str,
                    },
                },
            )
        return
    if etype == "result":
        _log.info(
            "claude result is_error=%s cost=%.6f duration=%sms turns=%s",
            bool(event.get("is_error")),
            float(event.get("total_cost_usd") or 0.0),
            event.get("duration_ms"),
            event.get("num_turns"),
            extra={
                "source": "claude_stdout",
                "step_id": step_id,
                "payload": {
                    "event": "result",
                    "is_error": event.get("is_error"),
                    "session_id": event.get("session_id"),
                    "total_cost_usd": event.get("total_cost_usd"),
                    "duration_ms": event.get("duration_ms"),
                    "num_turns": event.get("num_turns"),
                },
            },
        )
        return
    _log.debug(
        "claude event %s",
        etype or "unknown",
        extra={"source": "claude_stdout", "step_id": step_id, "payload": {"event": etype, "raw": event}},
    )


def _write_stream_archive(
    handle: IO[str] | None,
    lock: threading.Lock | None,
    kind: str,
    line: str,
) -> None:
    """Append ``line`` to the per-step stream archive, thread-safe.

    Errors are swallowed: the archive is a best-effort debug aid; failing it
    must not crash a tick. ``kind`` is ``"stdout"`` or ``"stderr"`` so the
    operator can tell the streams apart in the merged file.
    """
    if handle is None or lock is None:
        return
    timestamp = datetime.now(UTC).strftime("%H:%M:%S.%f")[:-3]
    payload = line if line.endswith("\n") else line + "\n"
    record = f"{timestamp} {kind}: {payload}"
    with lock:
        try:
            handle.write(record)
            handle.flush()
        except (OSError, ValueError):
            return


def _read_stdout(
    stream: IO[str],
    *,
    raw_buffer: list[str],
    events: list[dict[str, Any]],
    step_id: int | None,
    result_seen: threading.Event | None = None,
    idle_pulse: threading.Event | None = None,
    stream_archive: IO[str] | None = None,
    stream_archive_lock: threading.Lock | None = None,
) -> None:
    try:
        for line in stream:
            raw_buffer.append(line)
            _write_stream_archive(stream_archive, stream_archive_lock, "stdout", line)
            if idle_pulse is not None:
                idle_pulse.set()
            text = line.strip()
            if not text:
                continue
            try:
                event = json.loads(text)
            except (ValueError, TypeError):
                _log.info(
                    text,
                    extra={"source": "claude_stdout", "step_id": step_id, "payload": {"event": "raw", "raw": text}},
                )
                continue
            if isinstance(event, dict):
                events.append(event)
                _log_event(event, step_id=step_id)
                if result_seen is not None and event.get("type") == "result":
                    result_seen.set()
            else:
                _log.info(
                    "claude non-dict event",
                    extra={"source": "claude_stdout", "step_id": step_id, "payload": {"event": "raw", "raw": event}},
                )
    finally:
        stream.close()


def _read_stderr(
    stream: IO[str],
    *,
    buffer: list[str],
    step_id: int | None,
    idle_pulse: threading.Event | None = None,
    stream_archive: IO[str] | None = None,
    stream_archive_lock: threading.Lock | None = None,
) -> None:
    try:
        for line in stream:
            buffer.append(line)
            _write_stream_archive(stream_archive, stream_archive_lock, "stderr", line)
            if idle_pulse is not None:
                idle_pulse.set()
            text = line.rstrip("\n")
            if not text:
                continue
            _log.info(text, extra={"source": "claude_stderr", "step_id": step_id})
    finally:
        stream.close()


def _force_kill(proc: subprocess.Popen, reason: str, *, step_id: int | None) -> int:
    """SIGTERM, then SIGKILL after a short grace; return the resulting rc.

    Output already buffered by the reader threads is preserved by the caller;
    we only collapse the subprocess. ``runuser`` forwards SIGTERM, so the
    privilege-drop wrapper does not block the signal path.
    """
    _log.warning(
        "claude subprocess force-terminated (%s); buffered output is preserved",
        reason,
        extra={"source": "claude_exit", "step_id": step_id, "payload": {"reason": reason}},
    )
    proc.terminate()
    try:
        return proc.wait(timeout=_TERM_GRACE_SECS)
    except subprocess.TimeoutExpired:
        proc.kill()
        return proc.wait()


def _wait_with_watchdog(
    proc: subprocess.Popen,
    *,
    overall_timeout: int,
    post_result_grace: float,
    idle_timeout: float | None,
    result_seen: threading.Event,
    idle_pulse: threading.Event,
    step_id: int | None,
) -> tuple[int, str | None]:
    """Wait for ``proc`` to exit, enforcing three independent watchdogs.

    1. ``overall_timeout`` -- hard cap matching the existing ``timeout`` budget.
    2. ``post_result_grace`` -- once a ``{"type":"result"}`` event has been
       observed, allow that long for the subprocess to exit on its own. Past
       that, force-terminate. Targets the Bun event-loop hang where claude has
       finished its work but a leaked active handle prevents process exit.
    3. ``idle_timeout`` -- kill if no output has been seen on either stream for
       that long. Defense in depth for hangs occurring BEFORE the result event.
       Disabled when ``None``.

    Returns ``(returncode, kill_reason)``. ``kill_reason`` is ``None`` for a
    natural exit, otherwise one of: ``post_result_grace``, ``idle_stdout``,
    ``overall_timeout``.
    """
    deadline = time.monotonic() + overall_timeout
    grace_deadline: float | None = None
    last_pulse_at = time.monotonic()
    while True:
        rc = proc.poll()
        if rc is not None:
            return rc, None
        now = time.monotonic()
        if now >= deadline:
            rc = _force_kill(proc, "overall_timeout", step_id=step_id)
            return rc, "overall_timeout"
        if result_seen.is_set():
            if grace_deadline is None:
                grace_deadline = now + post_result_grace
                _log.debug(
                    "claude result event seen; allowing %.1fs to exit before forcing termination",
                    post_result_grace,
                    extra={"source": "claude_exit", "step_id": step_id},
                )
            elif now >= grace_deadline:
                rc = _force_kill(proc, "post_result_grace", step_id=step_id)
                return rc, "post_result_grace"
        if idle_pulse.is_set():
            idle_pulse.clear()
            last_pulse_at = now
        elif idle_timeout is not None and (now - last_pulse_at) > idle_timeout:
            rc = _force_kill(proc, "idle_stdout", step_id=step_id)
            return rc, "idle_stdout"
        time.sleep(min(_WATCHDOG_POLL_SECS, max(0.0, deadline - now)))


def _format_argv_for_log(argv: list[str]) -> str:
    """Render ``argv`` as a paste-ready shell line, with the ``-p`` prompt body elided.

    The prompt is large (full agent context) and would flood the log; replacing
    it with a length placeholder keeps the wrapper command (``runuser`` / ``sudo``
    / ``--permission-mode`` / ``--model``) visible so an operator can replay it
    by hand to diagnose ``Permission denied`` and similar wrapper failures.
    """
    pieces: list[str] = []
    elide_next = False
    for arg in argv:
        if elide_next:
            pieces.append(shlex.quote(f"<prompt: {len(arg)} chars elided>"))
            elide_next = False
            continue
        pieces.append(shlex.quote(arg))
        if arg == "-p":
            elide_next = True
    return " ".join(pieces)


def _build_claude_argv(prompt: str, *, cwd: Path) -> tuple[list[str], dict[str, str]]:
    """Compose the ``claude`` argv and env overrides adapted to the host environment.

    Returns ``(argv, env_overrides)``. ``env_overrides`` is empty unless we wrap
    with ``runuser``; in that case it carries the env vars that must replace
    the parent's (currently just ``HOME``, see
    :func:`claude_env.autoclaude_subprocess_env_overrides` for why).

    - Drops ``--permission-mode bypassPermissions`` when ``defaultMode=auto`` is
      already configured (in user or project settings).
    - When running as root, provisions the ``autoclaude`` user/group, shares the
      claude config and repo, and wraps the argv to drop privileges via
      ``runuser`` (or ``sudo`` fallback).
    """
    argv: list[str] = [
        "claude",
        "-p",
        prompt,
        "--output-format",
        "stream-json",
        "--verbose",
        "--model",
        _AGENT_MODEL,
    ]
    env_overrides: dict[str, str] = {}
    bypass = claude_env.should_bypass_permissions(cwd=cwd)
    if bypass:
        argv.extend(["--permission-mode", "bypassPermissions"])
        claude_env.log_mode_once("[claude_env] permission_mode=bypassPermissions (no defaultMode=auto)")
    else:
        claude_env.log_mode_once("[claude_env] permission_mode=<unset> (defaultMode=auto detected)")
    # claude only refuses root + bypassPermissions; in auto mode root is fine and
    # we do not need to provision the autoclaude user / wrap with runuser.
    if bypass and claude_env.is_root():
        # Provisioning (user creation, ~/.claude chgrp, claude binary chgrp) is
        # the responsibility of `autoclaude init` -- doing it mid-tick would slow
        # the first step and surprise the operator. The only per-tick permission
        # work is `share_repo`, since the worktree path varies per tick.
        if not claude_env.autoclaude_user_exists():
            msg = (
                f"system user '{claude_env.AUTOCLAUDE_USER}' is not provisioned on this host, but "
                "is required to run claude as non-root. "
                "Run `autoclaude init --user-autoclaude` to create it (and adjust the necessary "
                "permissions) before starting ticks."
            )
            raise UserCreationError(msg)
        # Single entry point for the per-tick shares the wrapped claude
        # subprocess depends on (credentials, gh, worktree). Centralising
        # this guarantees that any future launch path that hits ``run_step``
        # cannot silently skip one of the helpers.
        claude_env.share_per_tick_for_autoclaude_user(cwd=cwd)
        argv = claude_env.wrap_for_user(argv)
        # Force HOME to the autoclaude user's actual home; --preserve-environment
        # would otherwise leak HOME=/root and the wrapped claude would lock the
        # same session files as the parent claude, hanging in epoll_wait forever.
        env_overrides.update(claude_env.autoclaude_subprocess_env_overrides())
        claude_env.log_mode_once(f"[claude_env] sandbox_user={claude_env.AUTOCLAUDE_USER} (host UID=0)")
    return argv, env_overrides


def _user_creation_failure(exc: UserCreationError, *, started: float, step_id: int | None) -> ClaudeResult:
    """Build a failed ``ClaudeResult`` when the privilege-drop precondition fails.

    Extracted from ``run_step`` so the latter stays under the ruff statement
    threshold and so the recovery path is unit-testable in isolation.
    """
    duration_ms = int((time.monotonic() - started) * 1000)
    message = str(exc)
    _log.error(
        "claude subprocess could not start: %s",
        message,
        extra={"source": "claude_exit", "step_id": step_id, "payload": {"returncode": -1, "duration_ms": duration_ms}},
    )
    return ClaudeResult(ok=False, stdout="", stderr=message, duration_ms=duration_ms, summary=message)


def run_step(  # noqa: C901, PLR0912, PLR0915 (subprocess lifecycle: setup, threads, wait, parse, return — splitting further hurts readability)
    prompt: str,
    *,
    cwd: Path,
    timeout: int = 3600,
    post_result_grace: float = _USE_DEFAULT,
    idle_timeout: float | None = _USE_DEFAULT,
    step_id: int | None = None,
    env: dict[str, str] | None = None,
) -> ClaudeResult:
    """Run ``claude -p <prompt>`` in ``cwd`` and stream events to the logger.

    ``env`` is layered on top of the parent process env so plug-and-play tool
    slash commands (``~/.claude/commands/<tool>.md``) can read tool-specific
    config (server URL, API key, agent_config_id) without per-tool wiring.

    ``post_result_grace`` and ``idle_timeout`` drive the parent-side watchdog
    that protects the tick from a Bun-side hang where claude has finished its
    work but never exits (see ``_wait_with_watchdog``). Both default to the
    module-level constants resolved at call time so tests and operators can
    re-tune them by patching the module without re-importing the function.
    """
    if post_result_grace is _USE_DEFAULT:
        post_result_grace = _DEFAULT_POST_RESULT_GRACE_SECS
    if idle_timeout is _USE_DEFAULT:
        idle_timeout = _DEFAULT_IDLE_TIMEOUT_SECS
    started = time.monotonic()
    stdout_buffer: list[str] = []
    stderr_buffer: list[str] = []
    events: list[dict[str, Any]] = []
    result_seen = threading.Event()
    idle_pulse = threading.Event()
    subprocess_env = os.environ.copy()
    if env:
        subprocess_env.update(env)
    try:
        argv, env_overrides = _build_claude_argv(prompt, cwd=cwd)
    except UserCreationError as exc:
        return _user_creation_failure(exc, started=started, step_id=step_id)
    # env_overrides is non-empty only in the autoclaude-wrap branch; it forces
    # HOME=/home/autoclaude to prevent the wrapped claude from locking the
    # parent claude's session files. See ``_build_claude_argv``.
    subprocess_env.update(env_overrides)
    formatted_argv = _format_argv_for_log(argv)
    _log.debug(
        "claude exec: %s",
        formatted_argv,
        extra={"source": "claude_exec", "step_id": step_id, "payload": {"argv": argv}},
    )
    # Per-step stream archive: raw stdout + stderr of the claude subprocess so
    # an operator can replay exactly what came out of the child, untransformed
    # by event parsing. Errors opening the archive must not abort the tick;
    # we degrade to no-archive and log a warning.
    stream_archive: IO[str] | None = None
    stream_archive_lock: threading.Lock | None = None
    stream_archive_path: Path | None = None
    try:
        stream_archive_path = allocate_stream_log_path(step_id=step_id)
        stream_archive = stream_archive_path.open("w", encoding="utf-8")
        stream_archive_lock = threading.Lock()
        stream_archive.write(
            f"# claude stream archive\n"
            f"# started_at: {datetime.now(UTC).isoformat()}\n"
            f"# step_id: {step_id}\n"
            f"# argv: {formatted_argv}\n"
            f"# cwd: {cwd}\n"
            f"# ---\n",
        )
        stream_archive.flush()
    except OSError as exc:
        _log.warning(
            "failed to open claude stream archive at %s: %s",
            stream_archive_path,
            exc,
            extra={"source": "claude_exec", "step_id": step_id},
        )
        if stream_archive is not None:
            with contextlib.suppress(OSError):
                stream_archive.close()
        stream_archive = None
        stream_archive_lock = None
    proc = subprocess.Popen(
        argv,
        cwd=str(cwd),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
        env=subprocess_env,
    )
    # Snapshot the calling context (e.g. the active profile contextvar) so the
    # reader threads emit log lines tagged with the same profile as the parent
    # tick. Without this, threads start with the contextvar default and every
    # claude subprocess line shows up as ``[-]``. A ``Context`` cannot be
    # entered from two threads at once, so each reader gets its own copy.
    stdout_ctx = contextvars.copy_context()
    stderr_ctx = contextvars.copy_context()
    stdout_thread = threading.Thread(
        target=stdout_ctx.run,
        args=(_read_stdout, proc.stdout),
        kwargs={
            "raw_buffer": stdout_buffer,
            "events": events,
            "step_id": step_id,
            "result_seen": result_seen,
            "idle_pulse": idle_pulse,
            "stream_archive": stream_archive,
            "stream_archive_lock": stream_archive_lock,
        },
        daemon=True,
    )
    stderr_thread = threading.Thread(
        target=stderr_ctx.run,
        args=(_read_stderr, proc.stderr),
        kwargs={
            "buffer": stderr_buffer,
            "step_id": step_id,
            "idle_pulse": idle_pulse,
            "stream_archive": stream_archive,
            "stream_archive_lock": stream_archive_lock,
        },
        daemon=True,
    )
    stdout_thread.start()
    stderr_thread.start()
    try:
        returncode, kill_reason = _wait_with_watchdog(
            proc,
            overall_timeout=timeout,
            post_result_grace=post_result_grace,
            idle_timeout=idle_timeout,
            result_seen=result_seen,
            idle_pulse=idle_pulse,
            step_id=step_id,
        )
        stdout_thread.join(timeout=5)
        stderr_thread.join(timeout=5)
    finally:
        if stream_archive is not None:
            with contextlib.suppress(OSError):
                stream_archive.close()
    duration_ms = int((time.monotonic() - started) * 1000)
    stdout_text = "".join(stdout_buffer)
    stderr_text = "".join(stderr_buffer)
    if kill_reason == "overall_timeout":
        stderr_text += "\n[autoclaude] subprocess timed out"
    elif kill_reason == "idle_stdout":
        stderr_text += "\n[autoclaude] subprocess killed: stdout idle"
    result_event = _find_result_event(events)
    init_event = _find_init_event(events)
    session_id = ""
    cost = 0.0
    tokens = 0
    if isinstance(result_event, dict):
        session_id = str(result_event.get("session_id") or "")
        try:
            cost = float(result_event.get("total_cost_usd") or 0.0)
        except (TypeError, ValueError):
            cost = 0.0
        tokens = _extract_token_total(result_event)
    if not session_id and isinstance(init_event, dict):
        session_id = str(init_event.get("session_id") or "")
    token_exhausted = detect_token_exhaustion(stdout_text, stderr_text, result_event)
    result_text = _extract_result_text(result_event) or _collect_assistant_text(events)
    fail_reason = _extract_fail_marker(result_text)
    is_error_flag = bool(result_event.get("is_error")) if isinstance(result_event, dict) else False
    summary = _build_short_summary(result_text, fail_reason=fail_reason, stderr=stderr_text, returncode=returncode)
    # ok semantics under the watchdog:
    # - post_result_grace: the work completed (we observed the result event);
    #   the kill is internal cleanup, so trust the result event flags.
    # - overall_timeout / idle_stdout: the work did not complete cleanly.
    # - natural exit: the historical rule (rc + result + bail marker) applies.
    if kill_reason == "post_result_grace":
        ok = not is_error_flag and not fail_reason
    elif kill_reason in ("overall_timeout", "idle_stdout"):
        ok = False
    else:
        ok = returncode == 0 and not is_error_flag and not fail_reason
    _log.info(
        "claude subprocess exited rc=%s in %sms cost=%.6f tokens=%s%s",
        returncode,
        duration_ms,
        cost,
        tokens,
        f" (force-terminated: {kill_reason})" if kill_reason else "",
        extra={
            "source": "claude_exit",
            "step_id": step_id,
            "payload": {
                "returncode": returncode,
                "duration_ms": duration_ms,
                "total_cost_usd": cost,
                "token_cost_estimate": tokens,
                "session_id": session_id,
                "is_error": is_error_flag,
                "fail_reason": fail_reason,
                "kill_reason": kill_reason,
                "stream_archive": str(stream_archive_path) if stream_archive_path else None,
            },
        },
    )
    # On any non-zero exit (including the runuser/sudo permission-denied family),
    # echo the exact wrapped command so the operator can copy-paste it and
    # replay the failure manually as root. The post-result kill is excluded
    # because the user-facing tick succeeded -- the rc only reflects SIGTERM.
    if returncode != 0 and kill_reason != "post_result_grace":
        _log.error(
            "claude exec failed rc=%s; command was: %s",
            returncode,
            formatted_argv,
            extra={"source": "claude_exec", "step_id": step_id, "payload": {"argv": argv, "returncode": returncode}},
        )
    return ClaudeResult(
        ok=ok,
        stdout=stdout_text,
        stderr=stderr_text,
        session_id=session_id,
        total_cost_usd=cost,
        token_cost_estimate=tokens,
        duration_ms=duration_ms,
        token_exhausted=token_exhausted,
        summary=summary,
        fail_reason=fail_reason,
        events=events,
    )


_USAGE_TOKEN_KEYS = (
    "input_tokens",
    "output_tokens",
    "cache_creation_input_tokens",
    "cache_read_input_tokens",
)


def _extract_token_total(parsed: dict) -> int:
    """Sum every known token category from a `claude` JSON payload."""
    candidates: list[dict] = []
    for key in ("usage", "token_usage"):
        nested = parsed.get(key)
        if isinstance(nested, dict):
            candidates.append(nested)
    message = parsed.get("message")
    if isinstance(message, dict):
        nested = message.get("usage")
        if isinstance(nested, dict):
            candidates.append(nested)
    candidates.append(parsed)

    total = 0
    for src in candidates:
        for key in _USAGE_TOKEN_KEYS:
            value = src.get(key)
            if isinstance(value, int) and value > 0:
                total += value
        if total > 0:
            return total
    return total


def _find_result_event(events: list[dict[str, Any]]) -> dict[str, Any] | None:
    for event in reversed(events):
        if isinstance(event, dict) and event.get("type") == "result":
            return event
    return None


def _find_init_event(events: list[dict[str, Any]]) -> dict[str, Any] | None:
    for event in events:
        if isinstance(event, dict) and event.get("type") == "system":
            return event
    return None


def _extract_result_text(parsed: dict | None) -> str:
    if not isinstance(parsed, dict):
        return ""
    raw = parsed.get("result")
    if isinstance(raw, str):
        return raw.strip()
    return ""


def _collect_assistant_text(events: list[dict[str, Any]]) -> str:
    """Fallback: concatenate the last assistant message's text blocks."""
    for event in reversed(events):
        if not isinstance(event, dict) or event.get("type") != "assistant":
            continue
        msg = event.get("message") or {}
        parts = [
            block.get("text") or ""
            for block in (msg.get("content") or [])
            if isinstance(block, dict) and block.get("type") == "text"
        ]
        joined = "\n".join(p for p in parts if p)
        if joined:
            return joined.strip()
    return ""


def _extract_fail_marker(result_text: str) -> str:
    if not result_text:
        return ""
    match = _BAIL_MARKER_RE.search(result_text)
    if match is None:
        return ""
    return match.group("reason").strip()


def _build_short_summary(
    result_text: str,
    *,
    fail_reason: str,
    stderr: str,
    returncode: int,
) -> str:
    if fail_reason:
        return _truncate(fail_reason)
    candidate = _first_paragraph(result_text)
    if candidate:
        return _truncate(candidate)
    stderr_line = _first_line(stderr)
    if stderr_line:
        return _truncate(stderr_line)
    return f"claude exited rc={returncode}"


def _first_paragraph(text: str) -> str:
    if not text:
        return ""
    for block in text.split("\n\n"):
        stripped = block.strip()
        if stripped:
            return stripped
    return ""


def _first_line(text: str) -> str:
    if not text:
        return ""
    for line in text.splitlines():
        stripped = line.strip()
        if stripped:
            return stripped
    return ""
