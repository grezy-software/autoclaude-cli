"""Tests for RepoStorage: auto-heal, migration, history, retention, locks."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest
from filelock import Timeout

from autoclaude.repo_config import Retention
from autoclaude.storage import (
    MANAGED_GITIGNORE,
    ROOT_NAME,
    SCHEMA_VERSION,
    InvalidToolSlugError,
    RepoStorage,
    validate_tool_slug,
)


def test_ensure_creates_skeleton(tmp_path: Path) -> None:
    storage = RepoStorage.from_repo(tmp_path)
    meta = storage.ensure()

    assert meta.schema_version == SCHEMA_VERSION
    root = tmp_path / ROOT_NAME
    assert root.exists()
    for sub in ("state", "cache", "logs", "reports", "tmp", "locks", "tools"):
        assert (root / sub).is_dir()
    assert (root / "cache" / "api_docs").is_dir()
    assert (root / "META.json").exists()
    assert (root / ".gitignore").read_text(encoding="utf-8").strip() == MANAGED_GITIGNORE.strip()
    assert "tools/" in MANAGED_GITIGNORE


def test_ensure_is_idempotent(tmp_path: Path) -> None:
    storage = RepoStorage.from_repo(tmp_path)
    storage.ensure()
    before = (storage.gitignore_path).read_text(encoding="utf-8")
    mtime_before = storage.gitignore_path.stat().st_mtime
    # Wait a tick so mtime would change if the file were rewritten.
    time.sleep(0.02)
    storage.ensure()
    assert storage.gitignore_path.read_text(encoding="utf-8") == before
    assert storage.gitignore_path.stat().st_mtime == mtime_before


def test_ensure_migrates_legacy_docs_layout(tmp_path: Path) -> None:
    ac = tmp_path / ROOT_NAME
    legacy = ac / "docs" / "api" / "ac_runner_context"
    legacy.mkdir(parents=True)
    (legacy / "get.md").write_text("# legacy docs", encoding="utf-8")
    (legacy / "get.etag").write_text("abc", encoding="utf-8")

    storage = RepoStorage.from_repo(tmp_path)
    meta = storage.ensure()

    assert meta.schema_version == SCHEMA_VERSION
    moved = storage.api_docs_dir / "ac_runner_context" / "get.md"
    assert moved.exists()
    assert moved.read_text(encoding="utf-8") == "# legacy docs"
    assert not (ac / "docs").exists()


def test_meta_bump_only_happens_once(tmp_path: Path) -> None:
    storage = RepoStorage.from_repo(tmp_path)
    storage.ensure()
    # Corrupt META.json and re-run; ensure should rewrite at current version.
    storage.meta_path.write_text("not json", encoding="utf-8")
    meta = storage.ensure()
    assert meta.schema_version == SCHEMA_VERSION
    parsed = json.loads(storage.meta_path.read_text(encoding="utf-8"))
    assert parsed == {"schema_version": SCHEMA_VERSION}


def test_clean_tmp_wipes_directory(tmp_path: Path) -> None:
    storage = RepoStorage.from_repo(tmp_path)
    storage.ensure()
    doomed = storage.tmp_dir / "nested" / "file.txt"
    doomed.parent.mkdir(parents=True)
    doomed.write_text("delete me", encoding="utf-8")
    storage.clean_tmp()
    assert storage.tmp_dir.exists()
    assert not doomed.exists()
    assert list(storage.tmp_dir.iterdir()) == []


def test_append_history_writes_ndjson(tmp_path: Path) -> None:
    storage = RepoStorage.from_repo(tmp_path)
    storage.ensure()
    storage.append_history({"event": "tick_open", "tick_id": 1})
    storage.append_history({"event": "step_closed", "tick_id": 1, "step_id": 7})
    lines = storage.history_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    first = json.loads(lines[0])
    assert first["event"] == "tick_open"
    assert first["tick_id"] == 1
    assert "ts" in first


def test_write_step_prompt_and_streams(tmp_path: Path) -> None:
    storage = RepoStorage.from_repo(tmp_path)
    storage.ensure()
    storage.write_step_prompt(tick_id=42, step_id=7, prompt="do the thing")
    storage.write_step_streams(tick_id=42, step_id=7, stdout="hello", stderr="warn")
    base = storage.step_dir(42, 7)
    assert (base / "prompt.md").read_text(encoding="utf-8") == "do the thing"
    assert (base / "stdout.log").read_text(encoding="utf-8") == "hello"
    assert (base / "stderr.log").read_text(encoding="utf-8") == "warn"


def test_write_and_read_last_tick(tmp_path: Path) -> None:
    storage = RepoStorage.from_repo(tmp_path)
    storage.ensure()
    assert storage.read_last_tick() is None
    payload = {"tick_id": 9, "status": "succeeded", "cost_usd": 0.123}
    storage.write_last_tick(payload)
    assert storage.read_last_tick() == payload


def test_tick_lock_contended(tmp_path: Path) -> None:
    storage = RepoStorage.from_repo(tmp_path)
    storage.ensure()
    first = storage.tick_lock()
    first.acquire(timeout=0.0)
    try:
        second = storage.tick_lock()
        with pytest.raises(Timeout):
            second.acquire(timeout=0.0)
    finally:
        first.release()


def test_prune_removes_old_log_ticks(tmp_path: Path) -> None:
    storage = RepoStorage.from_repo(tmp_path)
    storage.ensure()
    stale = storage.tick_dir(1)
    stale.mkdir(parents=True)
    (stale / "summary.json").write_text("{}", encoding="utf-8")
    fresh = storage.tick_dir(2)
    fresh.mkdir(parents=True)
    (fresh / "summary.json").write_text("{}", encoding="utf-8")
    # Backdate stale to 30 days ago.
    old = time.time() - 30 * 86400
    os.utime(stale, (old, old))
    for path in stale.rglob("*"):
        os.utime(path, (old, old))

    storage.prune(Retention(logs_days=7, reports_days=0, api_docs_days=0))

    assert not stale.exists()
    assert fresh.exists()


def test_resolve_safe_rejects_parent_traversal(tmp_path: Path) -> None:
    storage = RepoStorage.from_repo(tmp_path)
    storage.ensure()
    with pytest.raises(ValueError, match="parent traversal"):
        storage.resolve_safe("../etc/passwd")


def test_resolve_safe_rejects_absolute(tmp_path: Path) -> None:
    storage = RepoStorage.from_repo(tmp_path)
    storage.ensure()
    with pytest.raises(ValueError, match="absolute path"):
        storage.resolve_safe("/etc/passwd")


def test_resolve_safe_allows_nested(tmp_path: Path) -> None:
    storage = RepoStorage.from_repo(tmp_path)
    storage.ensure()
    target = storage.step_dir(1, 2) / "stdout.log"
    target.parent.mkdir(parents=True)
    target.write_text("ok", encoding="utf-8")
    resolved = storage.resolve_safe("logs/ticks/1/steps/2/stdout.log")
    assert resolved == target.resolve()


# --- per-tool memory (the SEOTool / PentestTool use case) ------------------


def test_validate_tool_slug_accepts_reasonable_slugs() -> None:
    for slug in ("seo", "pentest", "seo_tool", "pentest-tool", "tool123", "a1"):
        assert validate_tool_slug(slug) == slug


def test_validate_tool_slug_rejects_path_escapes_and_weird_chars() -> None:
    for bad in ("../evil", "seo/", "/seo", "SEO", "seo tool", "", ".", "seo.", "-seo", "seo-"):
        with pytest.raises(InvalidToolSlugError):
            validate_tool_slug(bad)


def test_tool_dir_creates_per_slug_directory(tmp_path: Path) -> None:
    storage = RepoStorage.from_repo(tmp_path)
    storage.ensure()
    seo = storage.tool_dir("seo")
    pentest = storage.tool_dir("pentest")
    assert seo == tmp_path / ROOT_NAME / "tools" / "seo"
    assert pentest == tmp_path / ROOT_NAME / "tools" / "pentest"
    assert seo.is_dir() and pentest.is_dir()


def test_tool_dir_rejects_unsafe_slug(tmp_path: Path) -> None:
    storage = RepoStorage.from_repo(tmp_path)
    storage.ensure()
    with pytest.raises(InvalidToolSlugError):
        storage.tool_dir("../etc")


def test_read_tool_memory_is_empty_when_missing(tmp_path: Path) -> None:
    storage = RepoStorage.from_repo(tmp_path)
    storage.ensure()
    assert storage.read_tool_memory("seo") == {}


def test_write_and_read_tool_memory_round_trip(tmp_path: Path) -> None:
    """Exercise the full 'have I tested this target?' flow a SEO/Pentest tool would use."""
    storage = RepoStorage.from_repo(tmp_path)
    storage.ensure()

    memory = storage.read_tool_memory("seo")
    memory.setdefault("tested", {})["sha-abc"] = {"at": "2026-04-23T10:00:00Z", "result": "pass"}
    storage.write_tool_memory("seo", memory)

    again = storage.read_tool_memory("seo")
    assert "sha-abc" in again["tested"]
    assert again["tested"]["sha-abc"]["result"] == "pass"

    # Later run: add another target without clobbering the first.
    again["tested"]["sha-def"] = {"at": "2026-04-24T10:00:00Z", "result": "fail"}
    storage.write_tool_memory("seo", again)

    final = storage.read_tool_memory("seo")
    assert set(final["tested"]) == {"sha-abc", "sha-def"}


def test_read_tool_memory_tolerates_corrupt_file(tmp_path: Path) -> None:
    storage = RepoStorage.from_repo(tmp_path)
    storage.ensure()
    storage.tool_memory_path("seo").write_text("garbage {", encoding="utf-8")
    # No raise: returns empty so the tool can rebuild.
    assert storage.read_tool_memory("seo") == {}


def test_tool_memory_is_isolated_per_slug(tmp_path: Path) -> None:
    storage = RepoStorage.from_repo(tmp_path)
    storage.ensure()
    storage.write_tool_memory("seo", {"owner": "seo"})
    storage.write_tool_memory("pentest", {"owner": "pentest"})
    assert storage.read_tool_memory("seo") == {"owner": "seo"}
    assert storage.read_tool_memory("pentest") == {"owner": "pentest"}


def test_tool_memory_supports_named_files(tmp_path: Path) -> None:
    """Tools may store multiple memory files (e.g. ``targets.json`` + ``config.json``)."""
    storage = RepoStorage.from_repo(tmp_path)
    storage.ensure()
    storage.write_tool_memory("seo", {"sha": "abc"}, name="targets.json")
    storage.write_tool_memory("seo", {"depth": 3}, name="config.json")
    assert storage.read_tool_memory("seo", name="targets.json") == {"sha": "abc"}
    assert storage.read_tool_memory("seo", name="config.json") == {"depth": 3}
    # The default memory.json is untouched.
    assert storage.read_tool_memory("seo") == {}


def test_tool_lock_blocks_concurrent_acquire(tmp_path: Path) -> None:
    storage = RepoStorage.from_repo(tmp_path)
    storage.ensure()
    first = storage.tool_lock("seo")
    first.acquire(timeout=0.0)
    try:
        second = storage.tool_lock("seo")
        with pytest.raises(Timeout):
            second.acquire(timeout=0.0)
        # A different slug gets its own lock and does not contend.
        other = storage.tool_lock("pentest")
        other.acquire(timeout=0.0)
        other.release()
    finally:
        first.release()


def test_tool_memory_survives_prune(tmp_path: Path) -> None:
    """Retention pruning must not touch ``tools/``; it is state, not cache."""
    storage = RepoStorage.from_repo(tmp_path)
    storage.ensure()
    storage.write_tool_memory("seo", {"tested": {"sha-abc": {"at": "2020-01-01"}}})
    # Backdate the memory file so any time-based pruning would catch it.
    old = time.time() - 365 * 86400
    for entry in storage.tool_dir("seo").rglob("*"):
        os.utime(entry, (old, old))
    os.utime(storage.tool_dir("seo"), (old, old))

    storage.prune(Retention(logs_days=1, reports_days=1, api_docs_days=1))

    assert storage.read_tool_memory("seo") == {"tested": {"sha-abc": {"at": "2020-01-01"}}}


def test_resolve_safe_allows_tool_paths(tmp_path: Path) -> None:
    """Debug file requests need to reach into ``tools/`` for operator inspection."""
    storage = RepoStorage.from_repo(tmp_path)
    storage.ensure()
    storage.write_tool_memory("seo", {"ok": True})
    resolved = storage.resolve_safe("tools/seo/memory.json")
    assert resolved.is_file()
    assert resolved.read_text(encoding="utf-8")
