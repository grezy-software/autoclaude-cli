"""Tests for per-step tool dispatch (`_run_tool_steps`)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from autoclaude.api_client import ApiError
from autoclaude.claude_proc import ClaudeResult
from autoclaude.runner import (
    KIND_TOOL,
    STATUS_TOKEN_EXHAUSTED,
    _build_tool_prompt,
    _resolve_tool_command,
    _resolve_tool_command_body,
    _run_tool_steps,
    _TickState,
)
from autoclaude.storage import RepoStorage

_DISCORD_BODY = """---
description: Post a message.
---

Run curl with $TOKEN.
"""


class _FakeClient:
    base_url = "https://fake.invalid"
    api_key = "fake-key"

    def __init__(self) -> None:
        self.opened: list[dict[str, Any]] = []
        self.closed: list[dict[str, Any]] = []
        self._counter = 5000

    def open_step(self, **kwargs: Any) -> dict[str, Any]:
        self.opened.append(kwargs)
        self._counter += 1
        return {"id": self._counter}

    def close_step(self, step_id: int, **kwargs: Any) -> dict[str, Any]:
        self.closed.append({"step_id": step_id, **kwargs})
        return {"id": step_id}


@pytest.fixture
def storage(tmp_path: Path) -> RepoStorage:
    s = RepoStorage.from_repo(tmp_path)
    s.ensure()
    return s


@pytest.fixture
def state() -> _TickState:
    return _TickState(tick_id=42)


def _install_run_step(monkeypatch: pytest.MonkeyPatch, result: ClaudeResult) -> list[str]:
    calls: list[str] = []

    def _stub(prompt: str, **_kwargs: Any) -> ClaudeResult:
        calls.append(prompt)
        return result

    monkeypatch.setattr("autoclaude.runner.run_step", _stub)
    return calls


def test_resolve_tool_command_uses_first_command_from_manifest(storage: RepoStorage) -> None:
    storage.write_tool_manifest("discord", {"commands": [{"name": "discord-post", "body": "x"}]})
    assert _resolve_tool_command(storage, "discord") == "discord-post"


def test_resolve_tool_command_falls_back_to_slug_when_no_manifest(storage: RepoStorage) -> None:
    assert _resolve_tool_command(storage, "discord") == "discord"


def test_build_tool_prompt_truncates_long_stdout() -> None:
    prompt = _build_tool_prompt(
        command="discord-post",
        agent_slug="issuer",
        summary="ok",
        stdout="x" * 100_000,
    )
    assert prompt.startswith("/discord-post")
    assert "Previous step: issuer" in prompt
    assert "Summary: ok" in prompt
    assert "[truncated]" in prompt


def test_build_tool_prompt_inlines_instructions() -> None:
    prompt = _build_tool_prompt(
        command="discord-post",
        agent_slug="issuer",
        summary="ok",
        stdout="hi",
        instructions="Run curl with $TOKEN.",
    )
    assert prompt.startswith("Tool: /discord-post")
    assert "--- instructions ---" in prompt
    assert "Run curl with $TOKEN." in prompt
    assert "--- stdout ---" in prompt


def test_resolve_tool_command_body_strips_frontmatter(storage: RepoStorage) -> None:
    storage.write_tool_manifest(
        "discord", {"commands": [{"name": "discord-post", "body": _DISCORD_BODY}]}
    )
    body = _resolve_tool_command_body(storage, "discord")
    assert body == "Run curl with $TOKEN."


def test_resolve_tool_command_body_returns_none_without_manifest(storage: RepoStorage) -> None:
    assert _resolve_tool_command_body(storage, "missing") is None


def test_resolve_tool_command_body_returns_none_when_body_missing(storage: RepoStorage) -> None:
    storage.write_tool_manifest("discord", {"commands": [{"name": "discord-post"}]})
    assert _resolve_tool_command_body(storage, "discord") is None


def test_run_tool_steps_skips_tool_with_no_body(
    monkeypatch: pytest.MonkeyPatch,
    storage: RepoStorage,
    state: _TickState,
    tmp_path: Path,
) -> None:
    storage.write_tool_manifest("discord", {"commands": [{"name": "discord-post"}]})
    calls = _install_run_step(monkeypatch, ClaudeResult(ok=True, stdout="x", stderr=""))
    client = _FakeClient()

    _run_tool_steps(
        client,
        state,
        [{"slug": "discord"}],
        repo_checkout=tmp_path,
        shutdown_requested={"value": False},
        storage=storage,
        parent_step={"agent_slug": "issuer"},
        parent_result=ClaudeResult(ok=True, stdout="ok", stderr="", summary="s"),
        start_ordinal=0,
    )

    assert calls == []
    assert client.opened == []


def test_run_tool_steps_dispatches_one_step_per_tool(
    monkeypatch: pytest.MonkeyPatch,
    storage: RepoStorage,
    state: _TickState,
    tmp_path: Path,
) -> None:
    storage.write_tool_manifest(
        "discord", {"commands": [{"name": "discord-post", "body": _DISCORD_BODY}]}
    )
    storage.write_tool_manifest(
        "blogger", {"commands": [{"name": "blogger", "body": "Write blog."}]}
    )
    _install_run_step(monkeypatch, ClaudeResult(ok=True, stdout="ok", stderr="", summary="posted"))
    client = _FakeClient()
    parent_step = {"agent_slug": "issuer", "agent_config_id": 7, "prompt": "first"}
    parent_result = ClaudeResult(ok=True, stdout="agent stdout", stderr="", summary="agent summary")

    last = _run_tool_steps(
        client,
        state,
        [{"slug": "discord"}, {"slug": "blogger"}],
        repo_checkout=tmp_path,
        shutdown_requested={"value": False},
        storage=storage,
        parent_step=parent_step,
        parent_result=parent_result,
        start_ordinal=10,
    )

    assert last == 11
    assert len(client.opened) == 2
    assert client.opened[0]["kind"] == KIND_TOOL
    assert client.opened[0]["agent_slug"] == "issuer"
    assert client.opened[0]["ordinal"] == 10
    assert client.opened[0]["action"] == "/discord-post"
    assert client.opened[1]["action"] == "/blogger"
    assert len(client.closed) == 2
    assert all(c["error_log"] == "" for c in client.closed)


def test_run_tool_steps_failure_is_non_fatal(
    monkeypatch: pytest.MonkeyPatch,
    storage: RepoStorage,
    state: _TickState,
    tmp_path: Path,
) -> None:
    storage.write_tool_manifest(
        "discord", {"commands": [{"name": "discord-post", "body": _DISCORD_BODY}]}
    )
    _install_run_step(
        monkeypatch,
        ClaudeResult(ok=False, stdout="", stderr="boom", summary="", fail_reason=""),
    )
    client = _FakeClient()

    last = _run_tool_steps(
        client,
        state,
        [{"slug": "discord"}],
        repo_checkout=tmp_path,
        shutdown_requested={"value": False},
        storage=storage,
        parent_step={"agent_slug": "issuer"},
        parent_result=ClaudeResult(ok=True, stdout="ok", stderr="", summary="s"),
        start_ordinal=3,
    )

    assert last == 3
    assert state.status != STATUS_TOKEN_EXHAUSTED
    assert state.error == ""
    assert client.closed[0]["error_log"] == "boom"


def test_run_tool_steps_token_exhaustion_bubbles_up(
    monkeypatch: pytest.MonkeyPatch,
    storage: RepoStorage,
    state: _TickState,
    tmp_path: Path,
) -> None:
    storage.write_tool_manifest(
        "discord", {"commands": [{"name": "discord-post", "body": _DISCORD_BODY}]}
    )
    storage.write_tool_manifest(
        "blogger", {"commands": [{"name": "blogger", "body": "Write blog."}]}
    )
    _install_run_step(
        monkeypatch,
        ClaudeResult(ok=False, stdout="", stderr="Credit balance is too low.", token_exhausted=True),
    )
    client = _FakeClient()

    _run_tool_steps(
        client,
        state,
        [{"slug": "discord"}, {"slug": "blogger"}],
        repo_checkout=tmp_path,
        shutdown_requested={"value": False},
        storage=storage,
        parent_step={"agent_slug": "issuer"},
        parent_result=ClaudeResult(ok=True, stdout="ok", stderr="", summary="s"),
        start_ordinal=0,
    )

    assert state.status == STATUS_TOKEN_EXHAUSTED
    # Second tool must not be dispatched after token exhaustion.
    assert len(client.opened) == 1


def test_run_tool_steps_skips_when_open_step_fails(
    monkeypatch: pytest.MonkeyPatch,
    storage: RepoStorage,
    state: _TickState,
    tmp_path: Path,
) -> None:
    storage.write_tool_manifest(
        "discord", {"commands": [{"name": "discord-post", "body": _DISCORD_BODY}]}
    )
    calls = _install_run_step(monkeypatch, ClaudeResult(ok=True, stdout="x", stderr=""))

    class _BrokenClient(_FakeClient):
        def open_step(self, **kwargs: Any) -> dict[str, Any]:  # noqa: ARG002
            raise ApiError("boom")

    client = _BrokenClient()
    _run_tool_steps(
        client,
        state,
        [{"slug": "discord"}],
        repo_checkout=tmp_path,
        shutdown_requested={"value": False},
        storage=storage,
        parent_step={"agent_slug": "issuer"},
        parent_result=ClaudeResult(ok=True, stdout="ok", stderr="", summary="s"),
        start_ordinal=0,
    )

    assert calls == [], "run_step must not fire when open_step fails"
