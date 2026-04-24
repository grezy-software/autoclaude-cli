"""Fulfil server-initiated :class:`DebugFileRequest` items during a tick.

The dashboard lets an operator ask for any file under ``.autoclaude/`` on
the currently-running repo. The CLI polls this queue between steps, reads
each requested file (with its own path-escape check, matching the server's
allowlist), and uploads the content back. Size-capped and failure-tolerant:
a bad path or missing file reports a ``reason`` rather than bailing the tick.

This keeps the protocol symmetric with the server-side validator in
``apps.autoclaude.services.debug_file_request_service`` -- any defect on one
side is caught by the other.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from autoclaude.logger import get_logger

if TYPE_CHECKING:
    from autoclaude.api_client import ApiClient
    from autoclaude.storage import RepoStorage

_log = get_logger("debug_files")

# Matches DEBUG_FILE_REQUEST_MAX_CONTENT_BYTES on the server. Keep in sync.
MAX_CONTENT_BYTES = 2 * 1024 * 1024


def fulfill_pending(client: ApiClient, storage: RepoStorage) -> int:
    """Poll pending requests and fulfil whatever we can read safely.

    Returns the number of requests processed (whether fulfilled or denied).
    Swallows all errors internally so a server blip never aborts the tick.
    """
    try:
        pending = client.debug_file_request_pending()
    except Exception as exc:  # noqa: BLE001 (best-effort polling)
        _log.debug("debug_file_request_pending poll failed: %s", exc, extra={"source": "cli"})
        return 0

    handled = 0
    for row in pending:
        request_id = row.get("id")
        relative_path = row.get("relative_path") or ""
        if not isinstance(request_id, int) or not relative_path:
            continue
        _fulfill_one(client, storage, request_id, relative_path)
        handled += 1
    return handled


def _fulfill_one(client: ApiClient, storage: RepoStorage, request_id: int, relative_path: str) -> None:
    content: str = ""
    truncated = False
    reason: str = ""
    try:
        target = storage.resolve_safe(relative_path)
    except ValueError as exc:
        reason = f"path_rejected: {exc}"
    else:
        if not target.exists() or target.is_dir():
            reason = "file_not_found"
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
                    truncated = truncated or True

    try:
        client.debug_file_request_fulfill(
            request_id,
            content=content,
            content_truncated=truncated,
            reason=reason,
        )
    except Exception as exc:  # noqa: BLE001 (best-effort polling)
        _log.debug("debug_file_request_fulfill %s failed: %s", request_id, exc, extra={"source": "cli"})
        return

    storage.append_history(
        {
            "event": "debug_file_request_fulfilled",
            "request_id": request_id,
            "relative_path": relative_path,
            "truncated": truncated,
            "reason": reason,
        },
    )


__all__ = ["MAX_CONTENT_BYTES", "fulfill_pending"]
