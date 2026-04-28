"""Per-tick log archive with bounded retention.

After a tick finishes, the runner copies its ``.autoclaude/`` directory into
``<AUTOCLAUDE_HOME>/tick_logs/<tick_id>/`` so the dashboard can still ask the
daemon for a file from a tick whose worktree has already been cleaned up.

Archives older than :data:`RETENTION_DAYS` are pruned the next time the
daemon polls or a new tick is archived. The retention window matches the
dashboard's gate so the UI hides the request form at the same time as the
logs disappear from disk.

Path resolution rejects absolute paths, ``..`` traversal, and any symlink
target that resolves outside the archive root, so a hostile request can
never read ``~/.ssh/id_rsa`` or any other host file.
"""

from __future__ import annotations

import shutil
import time
from pathlib import Path

from autoclaude.logger import get_logger
from autoclaude.workspace import workspace_home

_log = get_logger("tick_archive")

ARCHIVE_DIRNAME = "tick_logs"
RETENTION_DAYS = 7
RETENTION_SECONDS = RETENTION_DAYS * 24 * 60 * 60


def archive_root() -> Path:
    return workspace_home() / ARCHIVE_DIRNAME


def archive_dir(tick_id: int) -> Path:
    return archive_root() / str(int(tick_id))


def archive_tick_logs(tick_id: int, source_autoclaude_dir: Path) -> Path | None:
    """Copy ``<worktree>/.autoclaude`` into the retention area.

    Returns the destination path on success, ``None`` if the source is
    missing or copy fails. Best-effort: the runner must not fail a tick
    just because the archive could not be written.
    """
    if not source_autoclaude_dir.exists() or not source_autoclaude_dir.is_dir():
        return None
    dest = archive_dir(tick_id)
    try:
        if dest.exists():
            shutil.rmtree(dest, ignore_errors=True)
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(source_autoclaude_dir, dest, symlinks=False, ignore_dangling_symlinks=True)
    except OSError as exc:
        _log.warning("tick %s archive failed: %s", tick_id, exc, extra={"source": "cli"})
        return None
    return dest


def purge_expired(*, now: float | None = None) -> int:
    """Delete archives whose mtime is older than the retention window.

    Returns the number of directories removed. Safe to call from the
    daemon heartbeat loop on every tick.
    """
    root = archive_root()
    if not root.exists():
        return 0
    cutoff = (now if now is not None else time.time()) - RETENTION_SECONDS
    removed = 0
    for entry in root.iterdir():
        if not entry.is_dir():
            continue
        try:
            mtime = entry.stat().st_mtime
        except OSError:
            continue
        if mtime < cutoff:
            shutil.rmtree(entry, ignore_errors=True)
            removed += 1
    return removed


def resolve_archived_file(tick_id: int, relative_path: str) -> Path | None:
    """Return the resolved file under the archive, or ``None`` if missing.

    Raises ``ValueError`` if the ``relative_path`` is unsafe (absolute,
    contains ``..``, or escapes the archive root after symlink resolution).
    The archive is the only place a daemon-initiated lookup is allowed to
    read from, so the entire host filesystem is off-limits by construction.
    """
    candidate = Path(relative_path)
    if candidate.is_absolute():
        msg = f"absolute path not allowed: {relative_path!r}"
        raise ValueError(msg)
    if any(part == ".." for part in candidate.parts):
        msg = f"parent traversal not allowed: {relative_path!r}"
        raise ValueError(msg)

    base = archive_dir(tick_id)
    if not base.exists() or not base.is_dir():
        return None
    try:
        resolved_root = base.resolve()
    except OSError:
        return None
    try:
        resolved = (base / candidate).resolve()
    except OSError:
        return None
    try:
        resolved.relative_to(resolved_root)
    except ValueError as exc:
        msg = f"path escapes tick archive: {relative_path!r}"
        raise ValueError(msg) from exc
    if not resolved.exists() or not resolved.is_file():
        return None
    return resolved


__all__ = [
    "ARCHIVE_DIRNAME",
    "RETENTION_DAYS",
    "RETENTION_SECONDS",
    "archive_dir",
    "archive_root",
    "archive_tick_logs",
    "purge_expired",
    "resolve_archived_file",
]
