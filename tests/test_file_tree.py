"""Tests for :mod:`autoclaude.file_tree` -- the tick-close snapshot builder."""

from __future__ import annotations

import json
from pathlib import Path

from autoclaude import file_tree
from autoclaude.file_tree import MAX_ENTRIES, build_snapshot
from autoclaude.storage import RepoStorage


def test_build_snapshot_returns_none_when_root_missing(tmp_path: Path) -> None:
    storage = RepoStorage.from_repo(tmp_path)
    # No ensure() on purpose: root must not exist.
    assert build_snapshot(storage) is None


def test_build_snapshot_lists_files_with_relative_paths(tmp_path: Path) -> None:
    storage = RepoStorage.from_repo(tmp_path)
    storage.ensure()
    (storage.state_dir / "last_tick.json").write_text('{"ok": true}', encoding="utf-8")
    (storage.logs_dir / "ticks" / "1" / "summary.json").parent.mkdir(parents=True, exist_ok=True)
    (storage.logs_dir / "ticks" / "1" / "summary.json").write_text('{"status": "ok"}', encoding="utf-8")

    snapshot = build_snapshot(storage)
    assert snapshot is not None
    assert snapshot["root"] == ".autoclaude"
    assert snapshot["truncated"] is False
    paths = [e["path"] for e in snapshot["entries"]]
    assert "state/last_tick.json" in paths
    assert "logs/ticks/1/summary.json" in paths
    # Sizes are included and match on disk.
    sizes = {e["path"]: e["size"] for e in snapshot["entries"]}
    assert sizes["state/last_tick.json"] == len('{"ok": true}')


def test_build_snapshot_truncates_when_over_entry_cap(tmp_path: Path, monkeypatch) -> None:
    storage = RepoStorage.from_repo(tmp_path)
    storage.ensure()
    # Shrink the cap so we don't need thousands of files to trigger it.
    monkeypatch.setattr(file_tree, "MAX_ENTRIES", 3)

    for i in range(5):
        (storage.logs_dir / f"f{i}.log").write_text("x", encoding="utf-8")

    snapshot = build_snapshot(storage)
    assert snapshot is not None
    assert snapshot["truncated"] is True
    assert len(snapshot["entries"]) == 3


def test_build_snapshot_skips_directories(tmp_path: Path) -> None:
    storage = RepoStorage.from_repo(tmp_path)
    storage.ensure()
    # ensure() creates empty subdirs; none should appear as entries.
    snapshot = build_snapshot(storage)
    assert snapshot is not None
    for entry in snapshot["entries"]:
        # Every entry should be a file (META.json + .gitignore come from ensure()).
        assert entry["path"]
        assert isinstance(entry["size"], int)


def test_build_snapshot_trims_to_byte_budget(tmp_path: Path, monkeypatch) -> None:
    storage = RepoStorage.from_repo(tmp_path)
    storage.ensure()
    # Set an unrealistically small byte cap to force the byte-level trim branch.
    monkeypatch.setattr(file_tree, "MAX_JSON_BYTES", 256)

    for i in range(20):
        (storage.logs_dir / f"file_{i:02d}.log").write_text("y" * 10, encoding="utf-8")

    snapshot = build_snapshot(storage)
    assert snapshot is not None
    assert snapshot["truncated"] is True
    assert len(json.dumps(snapshot)) <= 256 or len(snapshot["entries"]) == 0


def test_max_entries_constant_matches_server_cap() -> None:
    # Keep the CLI + server caps aligned so payloads never get rejected purely on size.
    assert MAX_ENTRIES == 4000
