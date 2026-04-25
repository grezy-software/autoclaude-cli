"""Tests for token-exhaustion detection and runner behavior on resumption / heartbeats."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any, Self

import pytest

from autoclaude.api_client import ApiError
from autoclaude.claude_proc import ClaudeResult, detect_token_exhaustion
from autoclaude.runner import (
    EXIT_OK,
    EXIT_TOKEN_EXHAUSTED,
    _apply_resumption,
    _build_resumption_banner,
    _execute_steps,
    _TickState,
    run_tick,
)
from autoclaude.storage import RepoStorage
from autoclaude.workspace import AUTOCLAUDE_HOME_ENV, Workspace


def _make_source(path: Path) -> Path:
    """Return a minimal git repo suitable as a ``source_repo``."""
    path.mkdir(parents=True, exist_ok=True)
    for args in (
        ["init", "-q", "-b", "main"],
        ["config", "user.email", "test@example.com"],
        ["config", "user.name", "Test"],
    ):
        subprocess.run(["git", *args], cwd=str(path), check=True, capture_output=True)
    (path / "README.md").write_text("hi\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=str(path), check=True, capture_output=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=str(path), check=True, capture_output=True)
    return path


@pytest.fixture
def workspace_factory(tmp_path, monkeypatch):
    """Build a `for_local_path` workspace factory keyed off a fresh source repo.

    Production injects ``Workspace.for_github_repo`` as the factory; tests
    swap in this local-path variant so the runner's clone step works
    offline against a tmpdir-backed git repo. The fixture also isolates
    ``$AUTOCLAUDE_HOME`` so per-test state never leaks.
    """
    monkeypatch.setenv(AUTOCLAUDE_HOME_ENV, str(tmp_path / "ac-home"))
    source = _make_source(tmp_path / "src")
    home = tmp_path / "ac-home"

    def _factory(_github_repo: str) -> Workspace:
        return Workspace.for_local_path(source, home=home)

    return _factory


# --- detect_token_exhaustion ------------------------------------------------


@pytest.mark.parametrize(
    ("stdout", "stderr", "parsed", "expected"),
    [
        ("", "", None, False),
        ('{"result": "all good"}', "", {"result": "all good"}, False),
        ("", "Credit balance is too low. Please top up.", None, True),
        ("", "", {"error": "insufficient_quota"}, True),
        ("", "Claude AI usage limit reached; retry tomorrow.", None, True),
        ("", "", {"message": "You are out of credits."}, True),
        ("", "Some unrelated TypeError", None, False),
    ],
)
def test_detect_token_exhaustion(stdout: str, stderr: str, parsed: dict | None, *, expected: bool) -> None:
    assert detect_token_exhaustion(stdout, stderr, parsed) is expected


# --- _build_resumption_banner -----------------------------------------------


def test_build_resumption_banner_with_last_step() -> None:
    banner = _build_resumption_banner(
        {"tick_id": 42, "last_step": {"agent_slug": "issuer", "ordinal": 1, "summary": "picked issue #7"}},
    )
    assert "#42" in banner
    assert "issuer" in banner
    assert "ordinal 1" in banner
    assert "issue #7" in banner


def test_build_resumption_banner_without_last_step() -> None:
    banner = _build_resumption_banner({"tick_id": 99, "last_step": None})
    assert "#99" in banner
    assert "No prior step completed" in banner


def test_apply_resumption_prepends_to_first_step() -> None:
    steps = [{"agent_slug": "issuer", "prompt": "do the thing"}, {"agent_slug": "custom", "prompt": "do another"}]
    _apply_resumption(steps, {"tick_id": 3, "last_step": {"agent_slug": "issuer", "ordinal": 0, "summary": "x"}})
    assert steps[0]["prompt"].startswith("[Resuming abandoned tick #3.")
    assert "do the thing" in steps[0]["prompt"]
    assert steps[1]["prompt"] == "do another"


# --- runner: fake client / fake run_step ------------------------------------


class _FakeApiClient:
    """Minimal stand-in for ``ApiClient`` that records calls."""

    base_url = "https://fake.invalid"
    api_key = "fake-key"

    def __init__(
        self,
        tick_open_response: dict[str, Any],
        context_plan: dict[str, Any],
        *,
        github_repo: str = "fake-org/fake-repo",
    ) -> None:
        self._tick_open_response = tick_open_response
        self._context_plan = context_plan
        self._github_repo = github_repo
        self.heartbeat_calls: list[int] = []
        self.heartbeat_payloads: list[dict[str, Any]] = []
        self.open_step_calls: list[dict[str, Any]] = []
        self.close_step_calls: list[dict[str, Any]] = []
        self.close_tick_calls: list[dict[str, Any]] = []
        self._step_counter = 1000

    def context(self) -> dict[str, Any]:
        return {
            "plan": self._context_plan,
            "project": {"github_repo": self._github_repo},
        }

    def open_tick(self, *, runner_version: str, project_id: int | None = None) -> dict[str, Any]:  # noqa: ARG002
        return self._tick_open_response

    def tick_heartbeat(
        self,
        tick_id: int,
        *,
        token_cost_estimate: int | None = None,
        cost_usd: float | None = None,
    ) -> dict[str, Any]:
        self.heartbeat_calls.append(tick_id)
        self.heartbeat_payloads.append({"token_cost_estimate": token_cost_estimate, "cost_usd": cost_usd})
        return {"ok": True}

    def open_step(
        self,
        *,
        tick_id: int,
        agent_slug: str = "",
        ordinal: int,
        name: str,
        kind: str = "agent",
        action: str = "",
        started_at: Any = None,
    ) -> dict[str, Any]:
        self.open_step_calls.append(
            {
                "tick_id": tick_id,
                "agent_slug": agent_slug,
                "ordinal": ordinal,
                "name": name,
                "kind": kind,
                "action": action,
                "started_at": started_at,
            },
        )
        self._step_counter += 1
        return {"id": self._step_counter}

    def close_step(
        self,
        step_id: int,
        *,
        summary: str = "",
        error_log: str = "",
        cost_usd: float = 0.0,
        token_cost_estimate: int = 0,
        ended_at: Any = None,
    ) -> dict[str, Any]:
        self.close_step_calls.append(
            {
                "step_id": step_id,
                "summary": summary,
                "error_log": error_log,
                "cost_usd": cost_usd,
                "token_cost_estimate": token_cost_estimate,
                "ended_at": ended_at,
            },
        )
        return {"id": step_id}

    def close_tick(
        self,
        tick_id: int,
        *,
        status: str,
        outcome: str,
        error_log: str,
        cost_usd: float,
        token_cost_estimate: int = 0,
    ) -> dict[str, Any]:
        self.close_tick_calls.append(
            {
                "tick_id": tick_id,
                "status": status,
                "outcome": outcome,
                "error_log": error_log,
                "cost_usd": cost_usd,
                "token_cost_estimate": token_cost_estimate,
            },
        )
        return {}

    def post_tick_logs(self, tick_id: int, entries: list[dict[str, Any]]) -> dict[str, Any]:  # noqa: ARG002
        return {"accepted": len(entries), "submitted": len(entries)}

    def upload_tick_file_tree(self, tick_id: int, snapshot: dict[str, Any]) -> dict[str, Any]:  # noqa: ARG002
        return {}


@pytest.fixture
def fake_run_step(monkeypatch):
    """Return a factory that installs a deterministic ``run_step`` stub."""

    def _install(result_maker):
        calls: list[str] = []

        def _stub(prompt: str, *, cwd: Path, step_id: int | None = None, **_kwargs) -> ClaudeResult:  # noqa: ARG001
            calls.append(prompt)
            return result_maker(prompt)

        monkeypatch.setattr("autoclaude.runner.run_step", _stub)
        # Also neutralize TickLogger context so the runner doesn't try to
        # flush logs through an uninitialized uploader.
        monkeypatch.setattr(
            "autoclaude.runner.TickLogger",
            lambda *_args, **_kwargs: _NoopContext(),
        )
        return calls

    return _install


class _NoopContext:
    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_exc: object) -> None:
        return None


def _basic_steps() -> list[dict[str, Any]]:
    return [
        {"agent_slug": "issuer", "prompt": "first"},
        {"agent_slug": "custom", "prompt": "second"},
    ]


def test_run_tick_closes_with_token_exhausted_and_skips_later_steps(workspace_factory, fake_run_step) -> None:
    calls = fake_run_step(
        lambda _p: ClaudeResult(ok=False, stdout="", stderr="Credit balance is too low.", token_exhausted=True),
    )
    client = _FakeApiClient(
        tick_open_response={"id": 1, "plan": {"steps": _basic_steps()}},
        context_plan={"steps": _basic_steps()},
    )

    exit_code = run_tick(client, workspace_factory=workspace_factory)

    assert exit_code == EXIT_TOKEN_EXHAUSTED
    assert len(calls) == 1, "second step must not run after token exhaustion"
    assert len(client.close_tick_calls) == 1
    assert client.close_tick_calls[0]["status"] == "token_exhausted"


def test_run_tick_pings_heartbeat_before_open_and_between_steps(workspace_factory, fake_run_step) -> None:
    fake_run_step(lambda _p: ClaudeResult(ok=True, stdout="ok", stderr="", total_cost_usd=0.0))
    client = _FakeApiClient(
        tick_open_response={"id": 7, "plan": {"steps": _basic_steps()}},
        context_plan={"steps": _basic_steps()},
    )

    exit_code = run_tick(client, workspace_factory=workspace_factory)

    assert exit_code == EXIT_OK
    # 1 ping right after open_tick + 1 at the top of each step (2 steps).
    assert client.heartbeat_calls == [7, 7, 7]


def test_run_tick_tolerates_heartbeat_api_error(workspace_factory, fake_run_step, monkeypatch) -> None:
    fake_run_step(lambda _p: ClaudeResult(ok=True, stdout="ok", stderr=""))

    client = _FakeApiClient(
        tick_open_response={"id": 9, "plan": {"steps": _basic_steps()}},
        context_plan={"steps": _basic_steps()},
    )

    def _raise_heartbeat(_tick_id: int, **_kwargs) -> dict[str, Any]:
        msg = "gateway unreachable"
        raise ApiError(msg)

    monkeypatch.setattr(client, "tick_heartbeat", _raise_heartbeat)

    exit_code = run_tick(client, workspace_factory=workspace_factory)

    assert exit_code == EXIT_OK
    assert len(client.close_tick_calls) == 1
    assert client.close_tick_calls[0]["status"] == "succeeded"


def test_run_tick_applies_resumption_banner_to_first_step(workspace_factory, fake_run_step) -> None:
    seen_prompts: list[str] = []
    fake_run_step(
        lambda prompt: seen_prompts.append(prompt) or ClaudeResult(ok=True, stdout="ok", stderr=""),
    )
    resumed_steps = _basic_steps()
    client = _FakeApiClient(
        tick_open_response={
            "id": 11,
            "plan": {"steps": resumed_steps},
            "resumed_from": {
                "tick_id": 10,
                "last_step": {"agent_slug": "issuer", "ordinal": 0, "summary": "completed plan"},
            },
        },
        context_plan={"steps": _basic_steps()},
    )

    exit_code = run_tick(client, workspace_factory=workspace_factory)

    assert exit_code == EXIT_OK
    assert seen_prompts[0].startswith("[Resuming abandoned tick #10.")
    assert "first" in seen_prompts[0]
    # Second step prompt is untouched.
    assert seen_prompts[1] == "second"


def test_execute_steps_abandons_when_shutdown_flag_is_set(tmp_path, monkeypatch) -> None:
    """Directly test the loop's shutdown check.

    SIGINT itself is flaky in pytest; the flag-level test is what actually
    guards the behavior in prod (the handler just flips this flag).
    """

    def _stub(prompt: str, **_kwargs) -> ClaudeResult:  # noqa: ARG001
        msg = "run_step should not be called"
        raise AssertionError(msg)

    monkeypatch.setattr("autoclaude.runner.run_step", _stub)

    client = _FakeApiClient(
        tick_open_response={"id": 5, "plan": {"steps": _basic_steps()}},
        context_plan={"steps": _basic_steps()},
    )
    state = _TickState(tick_id=5)
    shutdown = {"value": True}
    storage = RepoStorage.from_repo(tmp_path)
    storage.ensure()

    _execute_steps(client, state, _basic_steps(), tmp_path, shutdown, storage, start_ordinal=0)

    assert state.status == "abandoned"
    assert "shutdown" in state.error
    # The pre-step shutdown check must exit before calling open_step / run_step.
    assert client.open_step_calls == []
