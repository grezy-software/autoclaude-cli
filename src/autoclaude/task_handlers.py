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
from autoclaude.workspace import WORKTREES_DIRNAME, workspace_home

if TYPE_CHECKING:
    from autoclaude.api_client import ApiClient

_log = get_logger("task_handlers")

TaskHandler = Callable[["ApiClient", dict[str, Any]], dict[str, Any]]


def _resolve_tick_local_file(tick_id: int, relative_path: str) -> Path | None:
    """Best-effort lookup for a file in a known tick worktree.

    The daemon does not own a specific repo, so it searches every worktree
    the CLI has created for the given tick id. Returns ``None`` if no match
    is found (the daemon then denies the request rather than failing it).
    """
    worktrees_root = workspace_home() / WORKTREES_DIRNAME
    if not worktrees_root.exists():
        return None
    target_name = str(int(tick_id))
    for slug_dir in worktrees_root.iterdir():
        if not slug_dir.is_dir():
            continue
        candidate = slug_dir / target_name / ".autoclaude" / relative_path
        if candidate.exists() and candidate.is_file():
            return candidate
    return None


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

    target = _resolve_tick_local_file(tick_id, relative_path)
    content = ""
    truncated = False
    reason = ""
    if target is None:
        reason = "daemon_no_local_context"
    else:
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
