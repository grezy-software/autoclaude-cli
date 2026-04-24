"""Build a capped snapshot of the `.autoclaude/` folder layout.

Uploaded once at tick close so the dashboard can render a browsable tree in
the DebugFileRequest UI, rather than forcing the operator to guess paths.
Entries are sorted so the payload is stable across identical folders, which
keeps diffs readable when comparing two ticks side-by-side.

Caps mirror the server's ``FILE_TREE_MAX_ENTRIES`` / ``FILE_TREE_MAX_JSON_BYTES``:
we prune locally so the server does not reject a slightly-too-large payload
(a single dropped snapshot is worse for the UI than an incomplete one).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from autoclaude.logger import get_logger

if TYPE_CHECKING:
    from autoclaude.storage import RepoStorage

_log = get_logger("file_tree")

# Matches FILE_TREE_MAX_ENTRIES / FILE_TREE_MAX_JSON_BYTES on the server.
MAX_ENTRIES = 4000
MAX_JSON_BYTES = 512 * 1024


def build_snapshot(storage: RepoStorage) -> dict[str, Any] | None:
    """Walk ``storage.root`` and return a server-shaped tree snapshot.

    Returns ``None`` if the root is missing (shouldn't happen post-``ensure``).
    The payload is sorted by path and truncated to stay under the server's
    caps; ``truncated`` flags which limit was hit.
    """
    root = storage.root
    if not root.exists():
        return None

    entries: list[dict[str, Any]] = []
    truncated = False

    try:
        for path in sorted(root.rglob("*")):
            if not path.is_file():
                continue
            try:
                size = path.stat().st_size
            except OSError:
                continue
            try:
                rel = path.relative_to(root).as_posix()
            except ValueError:
                continue
            entries.append({"path": rel, "size": size})
    except OSError as exc:
        _log.debug("file tree walk failed: %s", exc, extra={"source": "cli"})
        return None

    if len(entries) > MAX_ENTRIES:
        entries = entries[:MAX_ENTRIES]
        truncated = True

    payload = {
        "generated_at": datetime.now(tz=UTC).isoformat().replace("+00:00", "Z"),
        "root": root.name,
        "truncated": truncated,
        "entries": entries,
    }

    while len(json.dumps(payload)) > MAX_JSON_BYTES and payload["entries"]:
        # Drop the deepest-looking entry first (longer paths tend to be log
        # dirs that blow up the byte budget; trimming from the tail is fine
        # because entries are already path-sorted).
        payload["entries"] = payload["entries"][:-1]
        payload["truncated"] = True

    return payload


__all__ = ["MAX_ENTRIES", "MAX_JSON_BYTES", "build_snapshot"]
