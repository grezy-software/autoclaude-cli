"""Invoke the ``claude`` CLI for one agent step and stream its output.

``run_step`` spawns ``claude -p <prompt> --output-format json`` via
``subprocess.Popen`` and tees each stdout/stderr line to the autoclaude
logger in real time. The full streams are still buffered in memory so
the final JSON can be parsed for ``session_id``, cost, and the
human-readable ``result`` text.

Failure detection has three layers:

1. ``returncode != 0`` -- the claude subprocess crashed.
2. ``is_error: true`` in the parsed JSON -- claude itself signalled an
   error (auth failure, rate limit, malformed prompt).
3. The ``[autoclaude:fail] <reason>`` marker in the ``result`` text --
   the agent chose to bail out on a task it could not complete. Agents
   are expected to emit this on its own line when they stop short.
"""

from __future__ import annotations

import json
import re
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import IO

from autoclaude.logger import get_logger

_log = get_logger("claude")

# All agent steps run on Opus: agents are the product surface, and the Haiku
# side-model that claude-code otherwise spins up for routing/summarisation adds
# a failure mode (e.g. image-fetching) we cannot debug from here. Worth the cost.
_AGENT_MODEL = "opus"

# Max chars for the per-step summary shipped to the dashboard. Kept short so
# the Steps table shows a single-line takeaway rather than a wall of text.
_SHORT_SUMMARY_CHARS = 240

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
    # One-line takeaway for the Steps table (capped at `_SHORT_SUMMARY_CHARS`).
    # Derived from the `result` field of the claude JSON, or the bail reason
    # when the agent raised the fail marker.
    summary: str = ""
    # Reason string extracted from the `[autoclaude:fail]` marker, when the
    # agent requested a tick-level failure. Empty otherwise.
    fail_reason: str = ""


def _tee_stream(stream: IO[str], source: str, *, buffer: list[str], step_id: int | None) -> None:
    try:
        for line in stream:
            buffer.append(line)
            text = line.rstrip("\n")
            if not text:
                continue
            _log.info(text, extra={"source": source, "step_id": step_id})
    finally:
        stream.close()


def run_step(
    prompt: str,
    *,
    cwd: Path,
    timeout: int = 3600,
    step_id: int | None = None,
) -> ClaudeResult:
    """Run ``claude -p <prompt>`` in ``cwd`` and tee output to the logger."""
    started = time.monotonic()
    stdout_buffer: list[str] = []
    stderr_buffer: list[str] = []
    proc = subprocess.Popen(
        [
            "claude",
            "-p",
            prompt,
            "--output-format",
            "json",
            "--model",
            _AGENT_MODEL,
        ],
        cwd=str(cwd),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
    )
    stdout_thread = threading.Thread(
        target=_tee_stream,
        args=(proc.stdout, "claude_stdout"),
        kwargs={"buffer": stdout_buffer, "step_id": step_id},
        daemon=True,
    )
    stderr_thread = threading.Thread(
        target=_tee_stream,
        args=(proc.stderr, "claude_stderr"),
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
        )
    stdout_thread.join(timeout=5)
    stderr_thread.join(timeout=5)
    duration_ms = int((time.monotonic() - started) * 1000)
    stdout_text = "".join(stdout_buffer)
    stderr_text = "".join(stderr_buffer)
    session_id, cost, tokens, parsed = _parse_result_metadata(stdout_text)
    token_exhausted = detect_token_exhaustion(stdout_text, stderr_text, parsed)
    result_text = _extract_result_text(parsed)
    fail_reason = _extract_fail_marker(result_text)
    is_error_flag = bool(parsed.get("is_error")) if isinstance(parsed, dict) else False
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
    )


_USAGE_TOKEN_KEYS = (
    "input_tokens",
    "output_tokens",
    "cache_creation_input_tokens",
    "cache_read_input_tokens",
)


def _extract_token_total(parsed: dict) -> int:
    """Sum every known token category from a `claude -p` JSON payload.

    The CLI's JSON has emitted the usage block in several shapes across versions:
    top-level keys, a nested `usage` dict, or a `message.usage` dict. Probe all
    three defensively and return 0 when nothing is found.
    """
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


def _parse_result_metadata(stdout_text: str) -> tuple[str, float, int, dict | None]:
    if not stdout_text:
        return "", 0.0, 0, None
    try:
        parsed = json.loads(stdout_text)
    except (ValueError, TypeError):
        return "", 0.0, 0, None
    if not isinstance(parsed, dict):
        return "", 0.0, 0, None
    session_id = str(parsed.get("session_id") or parsed.get("sessionId") or "")
    try:
        cost = float(parsed.get("total_cost_usd") or parsed.get("totalCostUsd") or 0.0)
    except (TypeError, ValueError):
        cost = 0.0
    tokens = _extract_token_total(parsed)
    return session_id, cost, tokens, parsed


def _extract_result_text(parsed: dict | None) -> str:
    """Pull the human-readable assistant message from a parsed claude JSON."""
    if not isinstance(parsed, dict):
        return ""
    raw = parsed.get("result")
    if isinstance(raw, str):
        return raw.strip()
    return ""


def _extract_fail_marker(result_text: str) -> str:
    """Return the reason string when the agent emitted ``[autoclaude:fail] ...``.

    Empty string when the marker is absent. The marker must appear on its own
    line to avoid false positives from prose that mentions the word ``fail``.
    """
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
    """Compose a one-line summary shown in the Steps table.

    Preference order: explicit bail reason, first non-empty paragraph of
    claude's ``result`` text, first line of stderr, then a generic
    ``rc=<n>`` fallback. Never returns raw JSON from stdout -- that used to
    leak into the dashboard when the result text was missing.
    """
    if fail_reason:
        return _truncate(fail_reason)
    candidate = _first_paragraph(result_text)
    if candidate:
        return _truncate(candidate)
    stderr_line = _first_line(stderr)
    if stderr_line:
        return _truncate(stderr_line)
    return f"claude exited rc={returncode}"


def _truncate(text: str) -> str:
    collapsed = " ".join(text.split())
    if len(collapsed) <= _SHORT_SUMMARY_CHARS:
        return collapsed
    return collapsed[: _SHORT_SUMMARY_CHARS - 1].rstrip() + "…"


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
