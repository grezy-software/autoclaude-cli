"""Handlers for RunnerTasks delivered through the daemon heartbeat.

Each handler takes the API client + the task's payload and returns a result
dict that is reported back via ``client.runner_task_complete``. Handlers
must be programmatic (no AI) and should swallow recoverable errors so the
daemon stays alive across machine sleep, network blips, etc.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

from autoclaude.debug_files import MAX_CONTENT_BYTES
from autoclaude.logger import get_logger
from autoclaude.tick_archive import resolve_archived_file

if TYPE_CHECKING:
    from autoclaude.api_client import ApiClient

_log = get_logger("task_handlers")

TaskHandler = Callable[["ApiClient", dict[str, Any]], dict[str, Any]]


def _resolve_tick_local_file(tick_id: int, relative_path: str) -> Path | None:
    """Look up a file in the per-tick log archive.

    The daemon only serves files copied into ``<AUTOCLAUDE_HOME>/tick_logs/``
    by ``runner._cleanup_worktree``. This is the single source of truth: the
    live worktree is never read directly, so a hostile ``relative_path`` can
    never reach repository sources, ssh keys, or other host files. Returns
    ``None`` if the tick has no archive (e.g. ran on a different machine, or
    archive was purged after the 7-day retention window).
    """
    return resolve_archived_file(tick_id, relative_path)


def handle_debug_file_fulfill(client: ApiClient, payload: dict[str, Any]) -> dict[str, Any]:
    """Resolve and upload a file requested by the dashboard.

    Best-effort: if the daemon cannot find a local worktree for the tick
    (e.g., the tick ran on a different machine, or the worktree has been
    garbage-collected), the underlying DebugFileRequest is marked denied
    with a clear reason rather than left hanging.
    """
    request_id = payload.get("debug_file_request_id")
    relative_path = payload.get("relative_path") or ""
    tick_id = payload.get("tick_id")
    if not isinstance(request_id, int) or not isinstance(tick_id, int) or not relative_path:
        msg = "missing debug_file_request_id, tick_id, or relative_path"
        raise ValueError(msg)

    content = ""
    truncated = False
    reason = ""
    target: Path | None
    try:
        target = _resolve_tick_local_file(tick_id, relative_path)
    except ValueError as exc:
        target = None
        reason = f"path_rejected: {exc}"
    if target is None and not reason:
        reason = "daemon_no_local_context"
    if target is not None:
        try:
            raw = target.read_bytes()
        except OSError as exc:
            reason = f"read_failed: {exc}"
        else:
            if len(raw) > MAX_CONTENT_BYTES:
                raw = raw[:MAX_CONTENT_BYTES]
                truncated = True
            try:
                content = raw.decode("utf-8")
            except UnicodeDecodeError:
                content = raw.decode("utf-8", errors="replace")
                truncated = True

    client.debug_file_request_fulfill(
        request_id,
        content=content,
        content_truncated=truncated,
        reason=reason,
    )
    return {
        "debug_file_request_id": request_id,
        "bytes": len(content.encode("utf-8")) if content else 0,
        "truncated": truncated,
        "denied_reason": reason,
    }


TASK_HANDLERS: dict[str, TaskHandler] = {
    "debug_file_fulfill": handle_debug_file_fulfill,
}


__all__ = ["TASK_HANDLERS", "TaskHandler", "handle_debug_file_fulfill"]
