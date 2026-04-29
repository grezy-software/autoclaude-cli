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

import json
import os
import re
import subprocess
import contextvars
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO, Any

from autoclaude.logger import get_logger

_log = get_logger("claude")

# All agent steps run on Opus: agents are the product surface, and the Haiku
# side-model that claude-code otherwise spins up for routing/summarisation adds
# a failure mode (e.g. image-fetching) we cannot debug from here. Worth the cost.
_AGENT_MODEL = "opus"

# Max chars for the per-step summary shipped to the dashboard. Kept short so
# the Steps table shows a single-line takeaway rather than a wall of text.
_SHORT_SUMMARY_CHARS = 240

# Truncation budgets for streamed event payloads. Keeps individual log rows
# small enough that long ticks do not blow out the backend.
_EVENT_SNIPPET_CHARS = 240
_EVENT_TEXT_BUDGET = 4000

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


def _truncate_text(text: str, limit: int = _EVENT_TEXT_BUDGET) -> str:
    if not text:
        return ""
    return text if len(text) <= limit else text[: limit - 1] + "…"


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


def _tool_input_summary(name: str, inp: dict[str, Any]) -> str:
    if not isinstance(inp, dict):
        return ""
    if name == "Bash":
        return _truncate(inp.get("command", ""), _EVENT_SNIPPET_CHARS)
    if name in {"Read", "Edit", "Write", "NotebookEdit"}:
        path = inp.get("file_path") or inp.get("notebook_path") or ""
        return _truncate(str(path), _EVENT_SNIPPET_CHARS)
    if name in {"Glob", "Grep"}:
        return _truncate(inp.get("pattern", ""), _EVENT_SNIPPET_CHARS)
    if name == "WebFetch":
        return _truncate(inp.get("url", ""), _EVENT_SNIPPET_CHARS)
    if name == "WebSearch":
        return _truncate(inp.get("query", ""), _EVENT_SNIPPET_CHARS)
    if name in {"Task", "Agent"}:
        return _truncate(inp.get("description") or inp.get("prompt", ""), _EVENT_SNIPPET_CHARS)
    try:
        return _truncate(json.dumps(inp, default=str), _EVENT_SNIPPET_CHARS)
    except (TypeError, ValueError):
        return ""


def _truncate_input(inp: Any) -> Any:
    """Cap a tool_use input dict so log payloads stay small."""
    try:
        encoded = json.dumps(inp, default=str)
    except (TypeError, ValueError):
        return {"_unserialisable": True}
    if len(encoded) <= _EVENT_TEXT_BUDGET:
        return inp
    return {"_truncated": True, "preview": encoded[: _EVENT_TEXT_BUDGET - 32]}


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
                    _truncate(text, _EVENT_SNIPPET_CHARS),
                    extra={
                        "source": "claude_stdout",
                        "step_id": step_id,
                        "payload": {"event": "assistant_text", "text": _truncate_text(text)},
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
                            "input": _truncate_input(block.get("input") or {}),
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
                _truncate(content_str, _EVENT_SNIPPET_CHARS),
                extra={
                    "source": "claude_stdout",
                    "step_id": step_id,
                    "payload": {
                        "event": "tool_result",
                        "tool_use_id": block.get("tool_use_id"),
                        "is_error": is_error,
                        "content": _truncate_text(content_str),
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


def _read_stdout(
    stream: IO[str],
    *,
    raw_buffer: list[str],
    events: list[dict[str, Any]],
    step_id: int | None,
) -> None:
    try:
        for line in stream:
            raw_buffer.append(line)
            text = line.strip()
            if not text:
                continue
            try:
                event = json.loads(text)
            except (ValueError, TypeError):
                _log.info(
                    text if len(text) <= _EVENT_SNIPPET_CHARS else text[: _EVENT_SNIPPET_CHARS - 1] + "…",
                    extra={"source": "claude_stdout", "step_id": step_id, "payload": {"event": "raw", "raw": text}},
                )
                continue
            if isinstance(event, dict):
                events.append(event)
                _log_event(event, step_id=step_id)
            else:
                _log.info(
                    "claude non-dict event",
                    extra={"source": "claude_stdout", "step_id": step_id, "payload": {"event": "raw", "raw": event}},
                )
    finally:
        stream.close()


def _read_stderr(stream: IO[str], *, buffer: list[str], step_id: int | None) -> None:
    try:
        for line in stream:
            buffer.append(line)
            text = line.rstrip("\n")
            if not text:
                continue
            _log.info(text, extra={"source": "claude_stderr", "step_id": step_id})
    finally:
        stream.close()


def run_step(
    prompt: str,
    *,
    cwd: Path,
    timeout: int = 3600,
    step_id: int | None = None,
    env: dict[str, str] | None = None,
) -> ClaudeResult:
    """Run ``claude -p <prompt>`` in ``cwd`` and stream events to the logger.

    ``env`` is layered on top of the parent process env so plug-and-play tool
    slash commands (``~/.claude/commands/<tool>.md``) can read tool-specific
    config (server URL, API key, agent_config_id) without per-tool wiring.
    """
    started = time.monotonic()
    stdout_buffer: list[str] = []
    stderr_buffer: list[str] = []
    events: list[dict[str, Any]] = []
    subprocess_env = os.environ.copy()
    if env:
        subprocess_env.update(env)
    proc = subprocess.Popen(
        [
            "claude",
            "-p",
            prompt,
            "--output-format",
            "stream-json",
            "--verbose",
            "--model",
            _AGENT_MODEL,
            "--permission-mode",
            "bypassPermissions",
        ],
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
        kwargs={"raw_buffer": stdout_buffer, "events": events, "step_id": step_id},
        daemon=True,
    )
    stderr_thread = threading.Thread(
        target=stderr_ctx.run,
        args=(_read_stderr, proc.stderr),
        kwargs={"buffer": stderr_buffer, "step_id": step_id},
        daemon=True,
    )
    stdout_thread.start()
    stderr_thread.start()
    try:
        returncode = proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
        stdout_thread.join(timeout=5)
        stderr_thread.join(timeout=5)
        stdout_text = "".join(stdout_buffer)
        stderr_text = "".join(stderr_buffer) + "\n[autoclaude] subprocess timed out"
        duration_ms = int((time.monotonic() - started) * 1000)
        _log.error(
            "claude subprocess timed out after %ss",
            timeout,
            extra={
                "source": "claude_exit",
                "step_id": step_id,
                "payload": {"returncode": -1, "duration_ms": duration_ms, "timed_out": True},
            },
        )
        return ClaudeResult(
            ok=False,
            stdout=stdout_text,
            stderr=stderr_text,
            duration_ms=duration_ms,
            events=events,
        )
    stdout_thread.join(timeout=5)
    stderr_thread.join(timeout=5)
    duration_ms = int((time.monotonic() - started) * 1000)
    stdout_text = "".join(stdout_buffer)
    stderr_text = "".join(stderr_buffer)
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
    ok = returncode == 0 and not is_error_flag and not fail_reason
    _log.info(
        "claude subprocess exited rc=%s in %sms cost=%.6f tokens=%s",
        returncode,
        duration_ms,
        cost,
        tokens,
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
            },
        },
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
