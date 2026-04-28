"""Unified `.autoclaude/` per-repo storage layout.

Every file the CLI writes inside a user's repo goes through ``RepoStorage``. It
owns the folder skeleton, an auto-managed ``.gitignore``, a schema-versioned
``META.json``, a repo-wide tick lock, an append-only history log, and retention
pruning driven by the per-repo ``config.toml``.

Layout
------

::

    .autoclaude/
    ├── .gitignore              (managed, committed)
    ├── META.json               (schema_version, committed)
    ├── config.toml             (optional, committed, see repo_config.py)
    ├── state/
    │   ├── attempts.json       (doc-protocol stage tracker)
    │   ├── attempts.json.lock
    │   └── last_tick.json      (most recent tick summary)
    ├── cache/
    │   └── api_docs/{slug}/{method}.{md,etag}
    ├── logs/
    │   ├── history.ndjson      (append-only audit trail)
    │   └── ticks/{tick_id}/
    │       ├── summary.json
    │       └── steps/{step_id}/{prompt.md,stdout.log,stderr.log}
    ├── reports/                (failure reports)
    ├── tmp/                    (scratch, wiped at tick start)
    ├── tools/                  (per-tool persistent memory)
    │   └── {slug}/memory.json  (see read_tool_memory / write_tool_memory)
    └── locks/
        ├── tick.lock
        └── tool-{slug}.lock    (optional, per-tool memory writes)

Only ``.gitignore``, ``META.json``, and (optionally) ``config.toml`` are
committed. Everything else is gitignored by the auto-managed file.
"""

from __future__ import annotations

import json
import re
import shutil
import tempfile
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from filelock import FileLock

if TYPE_CHECKING:
    from collections.abc import Iterator

    from autoclaude.repo_config import Retention

ROOT_NAME = ".autoclaude"
SCHEMA_VERSION = 1

# Subdir names are exposed as constants so tests and other modules can refer to
# them without duplicating the layout.
STATE_DIRNAME = "state"
CACHE_DIRNAME = "cache"
LOGS_DIRNAME = "logs"
REPORTS_DIRNAME = "reports"
TMP_DIRNAME = "tmp"
LOCKS_DIRNAME = "locks"
TOOLS_DIRNAME = "tools"

API_DOCS_SUBDIR = "api_docs"
TICKS_SUBDIR = "ticks"
STEPS_SUBDIR = "steps"

DEFAULT_TOOL_MEMORY_FILE = "memory.json"
TOOL_LOCK_FILE = "tool.lock"
_TOOL_SLUG_MAX_LENGTH = 64

META_FILE = "META.json"
GITIGNORE_FILE = ".gitignore"

ATTEMPTS_FILE = "attempts.json"
ATTEMPTS_LOCK = "attempts.json.lock"
LAST_TICK_FILE = "last_tick.json"
HISTORY_FILE = "history.ndjson"
TICK_LOCK_FILE = "tick.lock"
TOOL_HASHES_FILE = "tool_hashes.json"

_SUBDIRS = (
    STATE_DIRNAME,
    CACHE_DIRNAME,
    LOGS_DIRNAME,
    REPORTS_DIRNAME,
    TMP_DIRNAME,
    LOCKS_DIRNAME,
    TOOLS_DIRNAME,
)

_GITIGNORE_HEADER = "# Managed by autoclaude-cli. Do not edit by hand."
_GITIGNORE_BODY = """
state/
cache/
logs/
reports/
tmp/
locks/
tools/
""".lstrip()
MANAGED_GITIGNORE = f"{_GITIGNORE_HEADER}\n{_GITIGNORE_BODY}"

_HISTORY_SCHEMA_KEY = "schema_version"

_TOOL_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,62}[a-z0-9]$|^[a-z0-9]$")


class InvalidToolSlugError(ValueError):
    """Raised when a tool slug contains characters that could escape the tools/ dir."""


def validate_tool_slug(slug: str) -> str:
    """Return ``slug`` if it is safe to use as a subdirectory name.

    Rules: lowercase ``[a-z0-9_-]``, 1-64 chars, must start and end with an
    alphanumeric. This is stricter than POSIX filenames on purpose -- the slug
    doubles as a stable identifier the server shows in dashboards, so we reject
    anything that would look weird in a URL or path.
    """
    if not isinstance(slug, str):
        msg = f"tool slug must be a string, got {type(slug).__name__}"
        raise InvalidToolSlugError(msg)
    if len(slug) > _TOOL_SLUG_MAX_LENGTH:
        msg = f"tool slug exceeds {_TOOL_SLUG_MAX_LENGTH} chars: {slug!r}"
        raise InvalidToolSlugError(msg)
    if not _TOOL_SLUG_RE.match(slug):
        msg = f"tool slug must match [a-z0-9_-], got {slug!r}"
        raise InvalidToolSlugError(msg)
    return slug


def atomic_write_text(target: Path, content: str) -> None:
    """Write ``content`` to ``target`` atomically via tempfile + rename."""
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=str(target.parent),
        delete=False,
        prefix=".",
        suffix=".tmp",
    ) as handle:
        handle.write(content)
        tmp_path = Path(handle.name)
    tmp_path.replace(target)


def atomic_write_bytes(target: Path, content: bytes) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="wb",
        dir=str(target.parent),
        delete=False,
        prefix=".",
        suffix=".tmp",
    ) as handle:
        handle.write(content)
        tmp_path = Path(handle.name)
    tmp_path.replace(target)


def atomic_write_json(target: Path, data: Any) -> None:
    atomic_write_bytes(target, json.dumps(data, indent=2, default=str).encode("utf-8"))


@dataclass(frozen=True)
class Meta:
    schema_version: int = SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {"schema_version": self.schema_version}

    @classmethod
    def from_file(cls, path: Path) -> Meta:
        if not path.exists():
            return cls(schema_version=0)
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return cls(schema_version=0)
        if not isinstance(raw, dict):
            return cls(schema_version=0)
        version = raw.get(_HISTORY_SCHEMA_KEY)
        if not isinstance(version, int):
            return cls(schema_version=0)
        return cls(schema_version=version)


class RepoStorage:
    """Typed access to every file under ``<repo>/.autoclaude/``.

    Instances are cheap: construction does not touch the filesystem. Call
    :meth:`ensure` to auto-heal the folder (idempotent) before reading or
    writing. All CLI entry points run ``ensure()`` once on startup.
    """

    def __init__(self, ac_root: Path) -> None:
        self._root = ac_root

    # --- constructors ---------------------------------------------------------

    @classmethod
    def from_repo(cls, repo_root: Path) -> RepoStorage:
        """Build storage for a repo checkout (the dir containing ``.git/``)."""
        return cls(repo_root / ROOT_NAME)

    @classmethod
    def from_autoclaude_root(cls, ac_root: Path) -> RepoStorage:
        """Build storage when the caller already resolved the ``.autoclaude/`` path."""
        return cls(ac_root)

    # --- core paths -----------------------------------------------------------

    @property
    def root(self) -> Path:
        """The ``.autoclaude/`` directory itself."""
        return self._root

    @property
    def meta_path(self) -> Path:
        return self._root / META_FILE

    @property
    def gitignore_path(self) -> Path:
        return self._root / GITIGNORE_FILE

    @property
    def state_dir(self) -> Path:
        return self._root / STATE_DIRNAME

    @property
    def cache_dir(self) -> Path:
        return self._root / CACHE_DIRNAME

    @property
    def logs_dir(self) -> Path:
        return self._root / LOGS_DIRNAME

    @property
    def reports_dir(self) -> Path:
        return self._root / REPORTS_DIRNAME

    @property
    def tmp_dir(self) -> Path:
        return self._root / TMP_DIRNAME

    @property
    def locks_dir(self) -> Path:
        return self._root / LOCKS_DIRNAME

    @property
    def api_docs_dir(self) -> Path:
        return self.cache_dir / API_DOCS_SUBDIR

    @property
    def attempts_path(self) -> Path:
        return self.state_dir / ATTEMPTS_FILE

    @property
    def attempts_lock_path(self) -> Path:
        return self.state_dir / ATTEMPTS_LOCK

    @property
    def last_tick_path(self) -> Path:
        return self.state_dir / LAST_TICK_FILE

    @property
    def history_path(self) -> Path:
        return self.logs_dir / HISTORY_FILE

    @property
    def tick_lock_path(self) -> Path:
        return self.locks_dir / TICK_LOCK_FILE

    @property
    def tool_hashes_path(self) -> Path:
        return self.state_dir / TOOL_HASHES_FILE

    @property
    def tools_dir(self) -> Path:
        """Root of the per-tool persistent memory tree.

        Each tool owns ``tools/{slug}/`` and may put anything inside: a
        ``memory.json`` (see :meth:`read_tool_memory`), a SQLite database, a
        tree of cached artefacts, etc. The CLI never touches files here; they
        persist across ticks and are gitignored.
        """
        return self._root / TOOLS_DIRNAME

    def tool_dir(self, slug: str) -> Path:
        """Return (and create) the memory directory for ``slug``.

        Designed for tools like SEOTool / PentestTool that need to remember
        what they have already analysed. Typical use::

            memory = storage.read_tool_memory("seo")
            if target_sha in memory.get("tested", {}):
                return  # skip re-run
            memory.setdefault("tested", {})[target_sha] = {"at": now, "result": "pass"}
            storage.write_tool_memory("seo", memory)
        """
        safe = validate_tool_slug(slug)
        path = self.tools_dir / safe
        path.mkdir(parents=True, exist_ok=True)
        return path

    def tick_dir(self, tick_id: int) -> Path:
        return self.logs_dir / TICKS_SUBDIR / str(tick_id)

    def tick_summary_path(self, tick_id: int) -> Path:
        return self.tick_dir(tick_id) / "summary.json"

    def step_dir(self, tick_id: int, step_id: int) -> Path:
        return self.tick_dir(tick_id) / STEPS_SUBDIR / str(step_id)

    # --- lifecycle ------------------------------------------------------------

    def ensure(self) -> Meta:
        """Create the folder skeleton, migrate older layouts, refresh gitignore.

        Idempotent. Safe to call on every CLI invocation. Returns the current
        ``Meta`` (post-migration).
        """
        self._root.mkdir(parents=True, exist_ok=True)
        for subdir in _SUBDIRS:
            (self._root / subdir).mkdir(parents=True, exist_ok=True)
        self.api_docs_dir.mkdir(parents=True, exist_ok=True)
        self._write_gitignore_if_needed()
        meta = self._migrate()
        self._write_meta(meta)
        return meta

    def clean_tmp(self) -> None:
        """Wipe ``tmp/`` and recreate it empty. Called at tick start."""
        if self.tmp_dir.exists():
            shutil.rmtree(self.tmp_dir, ignore_errors=True)
        self.tmp_dir.mkdir(parents=True, exist_ok=True)

    def prune(self, retention: Retention) -> None:
        """Delete stale log/report/docs files per the retention policy."""
        now = time.time()
        self._prune_dir(self.logs_dir / TICKS_SUBDIR, now, retention.logs_days)
        self._prune_dir(self.reports_dir, now, retention.reports_days)
        self._prune_dir(self.api_docs_dir, now, retention.api_docs_days)

    # --- state helpers --------------------------------------------------------

    def state_lock(self) -> FileLock:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        return FileLock(str(self.attempts_lock_path))

    def tick_lock(self) -> FileLock:
        self.locks_dir.mkdir(parents=True, exist_ok=True)
        return FileLock(str(self.tick_lock_path))

    def write_last_tick(self, summary: dict[str, Any]) -> None:
        atomic_write_json(self.last_tick_path, summary)

    def read_last_tick(self) -> dict[str, Any] | None:
        if not self.last_tick_path.exists():
            return None
        try:
            data = json.loads(self.last_tick_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        return data if isinstance(data, dict) else None

    def read_tool_hashes(self) -> dict[str, str]:
        """Return the ``{slug: manifest_hash}`` map of last-applied tool manifests."""
        if not self.tool_hashes_path.exists():
            return {}
        try:
            data = json.loads(self.tool_hashes_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        if not isinstance(data, dict):
            return {}
        return {str(k): str(v) for k, v in data.items() if isinstance(v, str)}

    def write_tool_hashes(self, hashes: dict[str, str]) -> None:
        """Persist the ``{slug: manifest_hash}`` map atomically."""
        self.state_dir.mkdir(parents=True, exist_ok=True)
        atomic_write_json(self.tool_hashes_path, hashes)

    def tool_manifest_path(self, slug: str) -> Path:
        safe = validate_tool_slug(slug)
        return self.tool_dir(safe) / "manifest.json"

    def write_tool_manifest(self, slug: str, manifest: dict[str, Any]) -> Path:
        """Atomically persist a tool manifest body for later lookup (e.g. command names)."""
        path = self.tool_manifest_path(slug)
        atomic_write_json(path, manifest)
        return path

    def read_tool_manifest(self, slug: str) -> dict[str, Any] | None:
        """Return the cached manifest body for ``slug`` or ``None``."""
        path = self.tool_manifest_path(slug)
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        return data if isinstance(data, dict) else None

    def tool_memory_path(self, slug: str, *, name: str = DEFAULT_TOOL_MEMORY_FILE) -> Path:
        return self.tool_dir(slug) / name

    def read_tool_memory(self, slug: str, *, name: str = DEFAULT_TOOL_MEMORY_FILE) -> dict[str, Any]:
        """Return the tool's JSON memory, or ``{}`` if absent/corrupt.

        Corrupt files do not raise; tools that care can detect the empty dict
        and rebuild. This keeps a bad write from permanently breaking a tool.
        """
        path = self.tool_memory_path(slug, name=name)
        if not path.exists():
            return {}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return data if isinstance(data, dict) else {}

    def write_tool_memory(self, slug: str, data: dict[str, Any], *, name: str = DEFAULT_TOOL_MEMORY_FILE) -> Path:
        """Atomically persist the tool's JSON memory and return the target path."""
        path = self.tool_memory_path(slug, name=name)
        atomic_write_json(path, data)
        return path

    def tool_lock(self, slug: str) -> FileLock:
        """Per-tool lock for concurrent tools that write the same memory file."""
        safe = validate_tool_slug(slug)
        lock_path = self.locks_dir / f"tool-{safe}.lock"
        self.locks_dir.mkdir(parents=True, exist_ok=True)
        return FileLock(str(lock_path))

    # --- history (append-only ndjson) -----------------------------------------

    def append_history(self, event: dict[str, Any]) -> None:
        """Append a UTC-stamped JSON line to ``logs/history.ndjson``."""
        self.logs_dir.mkdir(parents=True, exist_ok=True)
        stamped = {"ts": datetime.now(tz=UTC).isoformat().replace("+00:00", "Z"), **event}
        with self.history_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(stamped, default=str) + "\n")

    # --- per-step artefacts ---------------------------------------------------

    def write_step_prompt(self, tick_id: int, step_id: int, prompt: str) -> Path:
        target = self.step_dir(tick_id, step_id) / "prompt.md"
        atomic_write_text(target, prompt)
        return target

    def write_step_streams(self, tick_id: int, step_id: int, *, stdout: str, stderr: str) -> tuple[Path, Path]:
        base = self.step_dir(tick_id, step_id)
        out = base / "stdout.log"
        err = base / "stderr.log"
        atomic_write_text(out, stdout)
        atomic_write_text(err, stderr)
        return out, err

    def write_tick_summary(self, tick_id: int, summary: dict[str, Any]) -> Path:
        target = self.tick_summary_path(tick_id)
        atomic_write_json(target, summary)
        return target

    # --- safe path resolution -------------------------------------------------

    def resolve_safe(self, relative_path: str) -> Path:
        """Resolve a relative path under the ``.autoclaude/`` root, rejecting escapes.

        Raises ``ValueError`` if the path is absolute, contains ``..`` components,
        or resolves outside the root. Used to gate file-read requests from the
        server so a hostile payload cannot read arbitrary files.
        """
        candidate = Path(relative_path)
        if candidate.is_absolute():
            msg = f"absolute path not allowed: {relative_path!r}"
            raise ValueError(msg)
        if any(part == ".." for part in candidate.parts):
            msg = f"parent traversal not allowed: {relative_path!r}"
            raise ValueError(msg)
        resolved_root = self._root.resolve()
        resolved = (self._root / candidate).resolve()
        try:
            resolved.relative_to(resolved_root)
        except ValueError as exc:
            msg = f"path escapes .autoclaude root: {relative_path!r}"
            raise ValueError(msg) from exc
        return resolved

    # --- internal helpers -----------------------------------------------------

    def _write_gitignore_if_needed(self) -> None:
        existing = ""
        if self.gitignore_path.exists():
            try:
                existing = self.gitignore_path.read_text(encoding="utf-8")
            except OSError:
                existing = ""
        if existing.strip() == MANAGED_GITIGNORE.strip():
            return
        atomic_write_text(self.gitignore_path, MANAGED_GITIGNORE)

    def _write_meta(self, meta: Meta) -> None:
        atomic_write_json(self.meta_path, meta.to_dict())

    def _migrate(self) -> Meta:
        """Upgrade older layouts to the current ``SCHEMA_VERSION``."""
        meta = Meta.from_file(self.meta_path)
        if meta.schema_version >= SCHEMA_VERSION:
            return meta
        # v0 -> v1: move docs/api/ into cache/api_docs/
        legacy_docs = self._root / "docs" / "api"
        if legacy_docs.exists() and not any(self.api_docs_dir.iterdir()):
            self.api_docs_dir.parent.mkdir(parents=True, exist_ok=True)
            for child in legacy_docs.iterdir():
                shutil.move(str(child), str(self.api_docs_dir / child.name))
            # Remove now-empty legacy tree.
            shutil.rmtree(self._root / "docs", ignore_errors=True)
        return Meta(schema_version=SCHEMA_VERSION)

    def _prune_dir(self, directory: Path, now: float, max_age_days: int) -> None:
        if max_age_days <= 0 or not directory.exists():
            return
        cutoff = now - max_age_days * 86400
        for entry in directory.iterdir():
            try:
                mtime = entry.stat().st_mtime
            except OSError:
                continue
            if mtime >= cutoff:
                continue
            if entry.is_dir():
                shutil.rmtree(entry, ignore_errors=True)
            else:
                try:
                    entry.unlink()
                except OSError:
                    continue


@contextmanager
def acquired_tick_lock(storage: RepoStorage, *, timeout: float = 0.0) -> Iterator[None]:
    """Acquire the repo-wide tick lock. Raises ``Timeout`` from filelock on contention."""
    lock = storage.tick_lock()
    with lock.acquire(timeout=timeout):
        yield


__all__ = [
    "ATTEMPTS_FILE",
    "ATTEMPTS_LOCK",
    "CACHE_DIRNAME",
    "DEFAULT_TOOL_MEMORY_FILE",
    "GITIGNORE_FILE",
    "HISTORY_FILE",
    "LAST_TICK_FILE",
    "LOCKS_DIRNAME",
    "LOGS_DIRNAME",
    "MANAGED_GITIGNORE",
    "META_FILE",
    "REPORTS_DIRNAME",
    "ROOT_NAME",
    "SCHEMA_VERSION",
    "STATE_DIRNAME",
    "TICK_LOCK_FILE",
    "TMP_DIRNAME",
    "TOOLS_DIRNAME",
    "InvalidToolSlugError",
    "Meta",
    "RepoStorage",
    "acquired_tick_lock",
    "atomic_write_bytes",
    "atomic_write_json",
    "atomic_write_text",
    "validate_tool_slug",
]
