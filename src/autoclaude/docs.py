"""Self-healing docs protocol for the AutoClaude server API.

The CLI surface for failing API calls is a 3-stage ladder per `(endpoint, method)`:

1. First failure -> attach local markdown (``cache/api_docs/<slug>/<method>.md``) if present, raise.
2. Second failure -> pull fresh markdown from ``<endpoint>docs/``, rewrite local cache, raise.
3. Third failure -> write a structured report JSON to ``reports/``, best-effort POST it to the
   server's ``/api/ac/runner/report/`` endpoint, raise a final ``stage="reported"`` error.

State persists across CLI invocations via ``state/attempts.json`` guarded by a file lock, with
a 24h TTL so a permanently stuck stage cannot block forever.

The stage transitions are *observed* by Claude (the caller reformats the payload between invocations
using the attached docs). The CLI itself does not retry.

Path layout lives in :mod:`autoclaude.storage`; this module takes a ``.autoclaude/`` root and
derives subpaths from the shared constants.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from filelock import FileLock

from autoclaude.storage import (
    API_DOCS_SUBDIR,
    ATTEMPTS_FILE,
    ATTEMPTS_LOCK,
    CACHE_DIRNAME,
    REPORTS_DIRNAME,
    STATE_DIRNAME,
    atomic_write_bytes,
    atomic_write_text,
)

if TYPE_CHECKING:  # pragma: no cover
    import httpx


STAGE_FRESH = "fresh"
STAGE_LOCAL = "local"
STAGE_REMOTE = "remote"
STAGE_REPORTED = "reported"

STAGE_ORDER = [STAGE_FRESH, STAGE_LOCAL, STAGE_REMOTE, STAGE_REPORTED]
STAGE_TTL_SECONDS = 24 * 3600

DOCS_DIR = f"{CACHE_DIRNAME}/{API_DOCS_SUBDIR}"
REPORTS_DIR = REPORTS_DIRNAME
STATE_DIR = STATE_DIRNAME


def endpoint_slug(docs_path: str) -> str:
    """Turn a logical endpoint path into a filesystem slug.

    ``/api/ac/runner/context/`` -> ``ac_runner_context``
    ``/api/ac/runner/tick_close/`` -> ``ac_runner_tick_close``
    """
    stripped = docs_path.strip("/").removeprefix("api/")
    return stripped.replace("/", "_")


def docs_url(docs_path: str) -> str:
    """Append ``docs/`` to a docs_path (which ends in ``/``)."""
    base = docs_path if docs_path.endswith("/") else f"{docs_path}/"
    return f"{base}docs/"


@dataclass(frozen=True)
class DocFetchResult:
    markdown: str
    etag: str


class DocProvider:
    """Read/write local docs, fetch remote with ETag caching."""

    def __init__(self, http: httpx.Client, root: Path) -> None:
        self._http = http
        self._root = root

    def local_path(self, docs_path: str, method: str) -> Path:
        return self._root / DOCS_DIR / endpoint_slug(docs_path) / f"{method.lower()}.md"

    def etag_path(self, docs_path: str, method: str) -> Path:
        return self.local_path(docs_path, method).with_suffix(".etag")

    def read_local(self, docs_path: str, method: str) -> str | None:
        path = self.local_path(docs_path, method)
        if not path.exists():
            return None
        return path.read_text(encoding="utf-8")

    def read_local_etag(self, docs_path: str, method: str) -> str | None:
        path = self.etag_path(docs_path, method)
        if not path.exists():
            return None
        return path.read_text(encoding="utf-8").strip() or None

    def fetch_remote(self, docs_path: str, method: str) -> DocFetchResult:
        url = docs_url(docs_path)
        headers: dict[str, str] = {}
        prior_etag = self.read_local_etag(docs_path, method)
        if prior_etag:
            headers["If-None-Match"] = f'"{prior_etag}"'
        response = self._http.get(url, headers=headers)
        if response.status_code == 304 and prior_etag:
            cached = self.read_local(docs_path, method) or ""
            return DocFetchResult(markdown=cached, etag=prior_etag)
        if response.status_code >= 400:
            msg = f"GET {url} -> {response.status_code} when fetching docs"
            raise DocFetchError(msg, status_code=response.status_code)
        markdown = response.text
        etag = response.headers.get("ETag", "").strip('"') or ""
        target = self.local_path(docs_path, method)
        atomic_write_text(target, markdown)
        if etag:
            atomic_write_text(self.etag_path(docs_path, method), etag)
        return DocFetchResult(markdown=markdown, etag=etag)


class DocFetchError(Exception):
    def __init__(self, message: str, *, status_code: int) -> None:
        super().__init__(message)
        self.status_code = status_code


class PersistentAttemptTracker:
    """Tracks the protocol stage per (endpoint, method) across processes.

    Storage is a JSON file with filelock-guarded atomic writes. Entries older than
    ``STAGE_TTL_SECONDS`` are auto-reset to ``fresh`` on read so a permanently
    stuck stage never blocks future calls.
    """

    def __init__(self, root: Path) -> None:
        self._root = root
        self._file = root / STATE_DIR / ATTEMPTS_FILE
        self._lock_path = root / STATE_DIR / ATTEMPTS_LOCK

    def _key(self, docs_path: str, method: str) -> str:
        return f"{endpoint_slug(docs_path)}:{method.lower()}"

    def _lock(self) -> FileLock:
        self._lock_path.parent.mkdir(parents=True, exist_ok=True)
        return FileLock(str(self._lock_path))

    def _load(self) -> dict[str, dict[str, str | float]]:
        if not self._file.exists():
            return {}
        try:
            return json.loads(self._file.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {}

    def _dump(self, data: dict[str, dict[str, str | float]]) -> None:
        atomic_write_bytes(self._file, json.dumps(data, indent=2).encode("utf-8"))

    def read(self, docs_path: str, method: str) -> str:
        key = self._key(docs_path, method)
        with self._lock():
            data = self._load()
            entry = data.get(key)
            if entry is None:
                return STAGE_FRESH
            updated_at = float(entry.get("updated_at", 0))
            if time.time() - updated_at > STAGE_TTL_SECONDS:
                return STAGE_FRESH
            stage = str(entry.get("stage", STAGE_FRESH))
            if stage not in STAGE_ORDER:
                return STAGE_FRESH
            return stage

    def write(self, docs_path: str, method: str, stage: str) -> None:
        key = self._key(docs_path, method)
        with self._lock():
            data = self._load()
            data[key] = {"stage": stage, "updated_at": time.time()}
            self._dump(data)

    def reset(self, docs_path: str, method: str) -> None:
        key = self._key(docs_path, method)
        with self._lock():
            data = self._load()
            if key in data:
                del data[key]
                self._dump(data)

    def snapshot(self) -> dict[str, str]:
        """Return current stage per endpoint (diag use only; does not apply TTL)."""
        with self._lock():
            data = self._load()
        return {k: str(v.get("stage", STAGE_FRESH)) for k, v in data.items()}


def next_stage(current: str) -> str:
    idx = STAGE_ORDER.index(current)
    return STAGE_ORDER[min(idx + 1, len(STAGE_ORDER) - 1)]


class ReportWriter:
    def __init__(self, root: Path) -> None:
        self._root = root

    def write(self, report: dict[str, object]) -> Path:
        slug = endpoint_slug(str(report.get("endpoint", "")))
        method = str(report.get("http_method", "unknown")).lower()
        stamp = datetime.now(tz=UTC).strftime("%Y%m%dT%H%M%SZ")
        target = self._root / REPORTS_DIR / f"{stamp}-{slug or 'unknown'}-{method}.json"
        atomic_write_bytes(target, json.dumps(report, indent=2, default=str).encode("utf-8"))
        return target

    def count(self) -> int:
        directory = self._root / REPORTS_DIR
        if not directory.exists():
            return 0
        return sum(1 for _ in directory.glob("*.json"))


__all__ = [
    "STAGE_FRESH",
    "STAGE_LOCAL",
    "STAGE_REMOTE",
    "STAGE_REPORTED",
    "STAGE_TTL_SECONDS",
    "DocFetchError",
    "DocFetchResult",
    "DocProvider",
    "PersistentAttemptTracker",
    "ReportWriter",
    "docs_url",
    "endpoint_slug",
    "next_stage",
]
