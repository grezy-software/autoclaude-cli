"""Queue-backed uploader that ships log records to the backend in batches.

Design
------

- One ``BackendLogHandler`` attached to the root ``autoclaude`` logger.
  Its ``emit`` is cheap: it stamps a monotonic ``client_seq`` per tick,
  serialises the record to a JSON-ready dict, and puts it on a
  ``queue.Queue``.
- One worker thread drains the queue, groups records into batches, and
  POSTs them via ``ApiClient.post_tick_logs``. On failure the batch is
  appended to an NDJSON sidecar file at
  ``~/.config/autoclaude/logs/pending-<tick_id>.ndjson``; the sidecar is
  replayed on the next successful batch and deleted once the backend
  acknowledges.
- Startup can replay any leftover ``pending-*.ndjson`` files from prior
  crashes against the current ApiClient -- see :func:`replay_pending`.
- ``flush(timeout=...)`` is synchronous: it waits for the queue to
  drain and the worker to report idle. Used by ``TickLogger`` on
  context exit and on crash handlers.

Threading model
---------------

Python's ``logging`` is thread-safe. The uploader uses one producer-side
lock only to advance ``client_seq`` -- everything else flows through the
queue and the single worker thread. ``httpx.Client`` is sync but used
only from the worker thread.
"""

from __future__ import annotations

import json
import logging
import queue
import threading
import time
import traceback
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from autoclaude.logger import LOGGER_NAME, get_logger, log_dir

if TYPE_CHECKING:
    from autoclaude.api_client import ApiClient

_DEFAULT_BATCH_SIZE = 100
_DEFAULT_FLUSH_INTERVAL = 3.0

_LEVEL_MAP = {
    logging.DEBUG: "info",
    logging.INFO: "info",
    logging.WARNING: "warning",
    logging.ERROR: "error",
    logging.CRITICAL: "error",
}


def pending_path(tick_id: int) -> Path:
    return log_dir() / f"pending-{tick_id}.ndjson"


def _record_to_entry(record: logging.LogRecord, client_seq: int) -> dict[str, Any]:
    """Convert a LogRecord into a serialisable payload for the backend."""
    extra = getattr(record, "__dict__", {})
    source = extra.get("source") or "cli"
    step_id = extra.get("step_id")
    payload_extra = extra.get("payload")
    payload: dict[str, Any] = dict(payload_extra) if isinstance(payload_extra, dict) else {}
    try:
        message = record.getMessage()
    except Exception:  # noqa: BLE001
        message = str(record.msg)
    if record.exc_info:
        payload["traceback"] = "".join(traceback.format_exception(*record.exc_info))
    ts = datetime.fromtimestamp(record.created, tz=UTC)
    return {
        "client_seq": client_seq,
        "level": _LEVEL_MAP.get(record.levelno, "info"),
        "source": source,
        "message": message,
        "payload": payload,
        "client_ts": ts.isoformat().replace("+00:00", "Z"),
        "step_id": int(step_id) if step_id is not None else None,
    }


@dataclass
class _Item:
    entry: dict[str, Any] | None  # None = sentinel to stop the worker


class BackendLogHandler(logging.Handler):
    """Forwards records to a ``BackendLogUploader``.

    Kept separate from the uploader so the handler can be attached to
    and detached from the root logger without touching the upload
    worker's lifecycle.
    """

    def __init__(self, uploader: BackendLogUploader) -> None:
        super().__init__(level=logging.INFO)
        self._uploader = uploader

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self._uploader.enqueue(record)
        except Exception:  # noqa: BLE001
            # Never let a logging failure kill the main program.
            self.handleError(record)


class BackendLogUploader:
    """Thread-backed uploader bound to a single tick."""

    def __init__(
        self,
        api_client: ApiClient,
        tick_id: int,
        *,
        batch_size: int = _DEFAULT_BATCH_SIZE,
        flush_interval: float = _DEFAULT_FLUSH_INTERVAL,
    ) -> None:
        self._api = api_client
        self._tick_id = tick_id
        self._batch_size = batch_size
        self._flush_interval = flush_interval
        self._queue: queue.Queue[_Item] = queue.Queue()
        self._seq_lock = threading.Lock()
        self._next_seq = 1
        self._stop = threading.Event()
        self._idle = threading.Event()
        self._idle.set()
        self._pending_path = pending_path(tick_id)
        self._internal_log = logging.getLogger(f"{LOGGER_NAME}._uploader")
        self._internal_log.propagate = False  # avoid feeding internal noise back into ourselves
        self._worker = threading.Thread(
            target=self._run,
            name=f"autoclaude-log-uploader-{tick_id}",
            daemon=True,
        )
        self._worker.start()

    @property
    def next_seq(self) -> int:
        """Peek at the next sequence number. For tests only."""
        return self._next_seq

    # -- producer side (called from any thread) --------------------------

    def enqueue(self, record: logging.LogRecord) -> None:
        """Stamp the record with a monotonic seq and queue it."""
        with self._seq_lock:
            seq = self._next_seq
            self._next_seq += 1
        entry = _record_to_entry(record, seq)
        self._idle.clear()
        self._queue.put(_Item(entry=entry))

    # -- worker side -----------------------------------------------------

    def _run(self) -> None:
        while not self._stop.is_set() or not self._queue.empty():
            try:
                item = self._queue.get(timeout=self._flush_interval)
            except queue.Empty:
                # Idle tick: retry any spilled batch then mark idle.
                self._replay_sidecar()
                self._idle.set()
                continue
            if item.entry is None:
                break
            self._idle.clear()
            batch: list[dict[str, Any]] = [item.entry]
            stop_after = False
            while len(batch) < self._batch_size:
                try:
                    nxt = self._queue.get_nowait()
                except queue.Empty:
                    break
                if nxt.entry is None:
                    stop_after = True
                    break
                batch.append(nxt.entry)
            self._send_or_spill(batch)
            if stop_after:
                break
            if self._queue.empty():
                self._idle.set()

    def _send_or_spill(self, batch: list[dict[str, Any]]) -> None:
        if not batch:
            return
        try:
            self._api.post_tick_logs(self._tick_id, batch)
        except Exception as exc:  # noqa: BLE001
            self._internal_log.warning("tick_log upload failed (%s); spilling %s entries to disk", exc, len(batch))
            self._spill(batch)
            return
        # On a successful send, try to drain any previously spilled batches.
        self._replay_sidecar()

    def _spill(self, batch: list[dict[str, Any]]) -> None:
        self._pending_path.parent.mkdir(parents=True, exist_ok=True)
        with self._pending_path.open("a", encoding="utf-8") as handle:
            for entry in batch:
                handle.write(json.dumps(entry) + "\n")

    def _replay_sidecar(self) -> None:
        if not self._pending_path.exists():
            return
        try:
            with self._pending_path.open(encoding="utf-8") as handle:
                entries = [json.loads(line) for line in handle if line.strip()]
        except (OSError, json.JSONDecodeError) as exc:
            self._internal_log.warning("cannot read pending log file %s: %s", self._pending_path, exc)
            return
        if not entries:
            self._pending_path.unlink(missing_ok=True)
            return
        try:
            for i in range(0, len(entries), self._batch_size):
                self._api.post_tick_logs(self._tick_id, entries[i : i + self._batch_size])
        except Exception:  # noqa: BLE001
            # Keep the file; retry on next flush.
            return
        self._pending_path.unlink(missing_ok=True)

    # -- public flush/close ---------------------------------------------

    def flush(self, timeout: float = 5.0) -> bool:
        """Block until the queue is empty and the worker is idle.

        Returns True if the worker reported idle within ``timeout``.
        """
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self._queue.empty() and self._idle.is_set():
                return True
            time.sleep(0.05)
        return self._queue.empty() and self._idle.is_set()

    def close(self, timeout: float = 5.0) -> None:
        """Stop the worker thread after flushing."""
        self.flush(timeout=timeout)
        self._stop.set()
        self._queue.put(_Item(entry=None))
        self._worker.join(timeout=timeout)


def replay_pending(api_client: ApiClient) -> int:
    """Replay any ``pending-*.ndjson`` files left by prior crashes.

    Called once on CLI startup (or before opening a new tick) to flush
    logs that were captured but never acknowledged by the backend.
    Returns the number of files successfully replayed and removed.
    """
    directory = log_dir()
    if not directory.exists():
        return 0
    log = get_logger("_uploader")
    log.propagate = False
    replayed = 0
    for path in directory.glob("pending-*.ndjson"):
        tick_id_str = path.stem.removeprefix("pending-")
        try:
            tick_id = int(tick_id_str)
        except ValueError:
            continue
        try:
            with path.open(encoding="utf-8") as handle:
                entries = [json.loads(line) for line in handle if line.strip()]
        except (OSError, json.JSONDecodeError):
            continue
        if not entries:
            path.unlink(missing_ok=True)
            continue
        try:
            for i in range(0, len(entries), _DEFAULT_BATCH_SIZE):
                api_client.post_tick_logs(tick_id, entries[i : i + _DEFAULT_BATCH_SIZE])
        except Exception as exc:  # noqa: BLE001
            log.debug("replay of %s failed (%s); keeping file for next run", path, exc)
            continue
        path.unlink(missing_ok=True)
        replayed += 1
    return replayed


__all__ = [
    "BackendLogHandler",
    "BackendLogUploader",
    "pending_path",
    "replay_pending",
]
