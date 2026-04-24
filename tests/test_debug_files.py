"""Tests for :mod:`autoclaude.debug_files` -- the CLI side of the debug-request protocol."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from autoclaude.debug_files import MAX_CONTENT_BYTES, fulfill_pending
from autoclaude.storage import RepoStorage


class _FakeClient:
    def __init__(self, pending: list[dict[str, Any]]) -> None:
        self._pending = pending
        self.fulfill_calls: list[dict[str, Any]] = []
        self.pending_calls = 0

    def debug_file_request_pending(self) -> list[dict[str, Any]]:
        self.pending_calls += 1
        return list(self._pending)

    def debug_file_request_fulfill(
        self,
        request_id: int,
        *,
        content: str = "",
        content_truncated: bool = False,
        reason: str = "",
    ) -> dict[str, Any]:
        self.fulfill_calls.append(
            {"request_id": request_id, "content": content, "content_truncated": content_truncated, "reason": reason},
        )
        return {"id": request_id, "status": "fulfilled" if not reason else "denied"}


def test_fulfill_pending_reads_and_uploads_file(tmp_path: Path) -> None:
    storage = RepoStorage.from_repo(tmp_path)
    storage.ensure()
    target = storage.last_tick_path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text('{"tick_id": 42}', encoding="utf-8")

    client = _FakeClient([{"id": 7, "relative_path": "state/last_tick.json"}])
    assert fulfill_pending(client, storage) == 1
    assert client.fulfill_calls == [
        {"request_id": 7, "content": '{"tick_id": 42}', "content_truncated": False, "reason": ""},
    ]


def test_fulfill_pending_reports_missing_file(tmp_path: Path) -> None:
    storage = RepoStorage.from_repo(tmp_path)
    storage.ensure()

    client = _FakeClient([{"id": 1, "relative_path": "state/last_tick.json"}])
    fulfill_pending(client, storage)
    call = client.fulfill_calls[0]
    assert call["content"] == ""
    assert call["reason"] == "file_not_found"


def test_fulfill_pending_rejects_path_escape(tmp_path: Path) -> None:
    storage = RepoStorage.from_repo(tmp_path)
    storage.ensure()

    client = _FakeClient([{"id": 2, "relative_path": "../etc/passwd"}])
    fulfill_pending(client, storage)
    call = client.fulfill_calls[0]
    assert call["content"] == ""
    assert "path_rejected" in call["reason"]


def test_fulfill_pending_truncates_oversize_file(tmp_path: Path) -> None:
    storage = RepoStorage.from_repo(tmp_path)
    storage.ensure()
    target = storage.last_tick_path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"A" * (MAX_CONTENT_BYTES + 100))

    client = _FakeClient([{"id": 3, "relative_path": "state/last_tick.json"}])
    fulfill_pending(client, storage)
    call = client.fulfill_calls[0]
    assert len(call["content"]) == MAX_CONTENT_BYTES
    assert call["content_truncated"] is True


def test_fulfill_pending_swallows_polling_errors(tmp_path: Path) -> None:
    class _BrokenClient:
        def debug_file_request_pending(self) -> list[dict[str, Any]]:
            msg = "gateway gone"
            raise RuntimeError(msg)

        def debug_file_request_fulfill(self, *_args, **_kwargs) -> dict[str, Any]:
            raise AssertionError("must not be called when polling fails")

    storage = RepoStorage.from_repo(tmp_path)
    storage.ensure()
    assert fulfill_pending(_BrokenClient(), storage) == 0


def test_fulfill_pending_appends_history_entry(tmp_path: Path) -> None:
    storage = RepoStorage.from_repo(tmp_path)
    storage.ensure()
    target = storage.last_tick_path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("ok", encoding="utf-8")

    client = _FakeClient([{"id": 42, "relative_path": "state/last_tick.json"}])
    fulfill_pending(client, storage)

    lines = storage.history_path.read_text(encoding="utf-8").splitlines()
    events = [json.loads(line) for line in lines]
    assert any(e.get("event") == "debug_file_request_fulfilled" and e.get("request_id") == 42 for e in events)
