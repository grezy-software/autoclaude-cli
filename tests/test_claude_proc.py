"""Tests for the claude subprocess runner."""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path

import pytest

from autoclaude import claude_env, claude_proc
from autoclaude.claude_env import UserCreationError
from autoclaude.claude_proc import (
    _SHORT_SUMMARY_CHARS,
    _build_claude_argv,
    _build_short_summary,
    _extract_fail_marker,
    _parse_result_metadata,
    run_step,
)


@pytest.fixture(autouse=True)
def _isolate_claude_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Default to a non-root, no-settings environment so existing tests stay deterministic."""
    claude_env.reset_caches()
    monkeypatch.setattr(claude_env.os, "geteuid", lambda: 1000)


def test_parse_result_metadata_reads_cost_and_session() -> None:
    blob = '{"session_id": "abc", "total_cost_usd": 0.0125}'
    session, cost, tokens, parsed = _parse_result_metadata(blob)
    assert session == "abc"
    assert cost == pytest.approx(0.0125)
    assert tokens == 0
    assert parsed is not None
    assert parsed["session_id"] == "abc"


def test_parse_result_metadata_handles_missing_fields() -> None:
    session, cost, tokens, parsed = _parse_result_metadata("not-json")
    assert session == ""
    assert cost == 0.0
    assert tokens == 0
    assert parsed is None


def test_parse_result_metadata_sums_nested_usage_tokens() -> None:
    blob = (
        '{"session_id": "abc", "total_cost_usd": 0.5, '
        '"usage": {"input_tokens": 100, "output_tokens": 50, '
        '"cache_creation_input_tokens": 20, "cache_read_input_tokens": 10}}'
    )
    _, _, tokens, _ = _parse_result_metadata(blob)
    assert tokens == 180


def test_parse_result_metadata_sums_top_level_usage_tokens() -> None:
    blob = '{"session_id": "abc", "input_tokens": 5, "output_tokens": 3}'
    _, _, tokens, _ = _parse_result_metadata(blob)
    assert tokens == 8


def test_run_step_tees_stdout_and_parses_cost(tmp_path, monkeypatch) -> None:
    """Use a fake `claude` that emits JSON with cost, and verify tee + parse."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    fake_claude_dir = tmp_path / "bin"
    fake_claude_dir.mkdir()
    fake_script = fake_claude_dir / "claude"
    # Echo the received argv to stderr so the test can assert `--model opus` is passed.
    fake_script.write_text(
        f"#!{sys.executable}\n"
        "import json, sys\n"
        'sys.stderr.write("argv=" + " ".join(sys.argv[1:]) + "\\n")\n'
        'sys.stderr.write("warmup line\\n")\n'
        'print(json.dumps({"session_id": "sess-1", "total_cost_usd": 0.42}))\n',
        encoding="utf-8",
    )
    fake_script.chmod(0o755)
    monkeypatch.setenv("PATH", f"{fake_claude_dir}:/usr/bin:/bin")

    cwd = tmp_path / "repo"
    cwd.mkdir()

    # Capture logs on the autoclaude.claude logger directly (propagate is False on parent).
    records: list[logging.LogRecord] = []

    class _Capture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    claude_logger = logging.getLogger("autoclaude.claude")
    capture = _Capture(level=logging.INFO)
    claude_logger.addHandler(capture)
    try:
        result = run_step("anything", cwd=cwd, step_id=123)
    finally:
        claude_logger.removeHandler(capture)

    assert result.ok
    assert result.session_id == "sess-1"
    assert result.total_cost_usd == pytest.approx(0.42)
    stdout_records = [r for r in records if getattr(r, "source", None) == "claude_stdout"]
    assert stdout_records, "expected subprocess stdout to be teed to the logger"
    stderr_records = [r for r in records if getattr(r, "source", None) == "claude_stderr"]
    assert stderr_records, "expected subprocess stderr to be teed to the logger"
    exit_records = [r for r in records if getattr(r, "source", None) == "claude_exit"]
    assert exit_records, "expected a claude_exit record to be emitted"
    argv_line = next(
        (r.getMessage() for r in stderr_records if r.getMessage().startswith("argv=")),
        "",
    )
    assert "--model opus" in argv_line, f"expected --model opus in argv, got: {argv_line}"


def test_extract_fail_marker_detects_agent_bailout() -> None:
    body = (
        "The `gh` CLI cannot operate on this repository.\n\n"
        "Per the ground rules, bailing out of this tick.\n\n"
        "[autoclaude:fail] No GitHub remote configured"
    )
    assert _extract_fail_marker(body) == "No GitHub remote configured"


def test_extract_fail_marker_requires_own_line() -> None:
    body = "some log that happens to include the word [autoclaude:fail] inline"
    assert _extract_fail_marker(body) == ""


def test_build_short_summary_prefers_bail_reason() -> None:
    summary = _build_short_summary(
        "long body of text that should not win over the explicit bail reason",
        fail_reason="auth failed",
        stderr="",
        returncode=0,
    )
    assert summary == "auth failed"


def test_build_short_summary_falls_back_to_first_paragraph() -> None:
    body = "First paragraph with the takeaway.\n\nSecond paragraph is details."
    summary = _build_short_summary(body, fail_reason="", stderr="", returncode=0)
    assert summary == "First paragraph with the takeaway."


def test_build_short_summary_collapses_whitespace_and_caps_length() -> None:
    body = "word " * 200
    summary = _build_short_summary(body, fail_reason="", stderr="", returncode=0)
    assert len(summary) <= _SHORT_SUMMARY_CHARS
    assert summary.endswith("…")


def test_build_short_summary_never_returns_raw_stdout() -> None:
    """Guard: when `result` text is missing the dashboard must not get JSON."""
    summary = _build_short_summary("", fail_reason="", stderr="", returncode=0)
    assert summary == "claude exited rc=0"
    assert "{" not in summary


def _write_fake_claude_emitting(payload: dict, tmp_path, monkeypatch) -> None:
    """Drop a fake `claude` executable on PATH that prints `payload` as JSON."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    fake_claude_dir = tmp_path / "bin"
    fake_claude_dir.mkdir()
    fake_script = fake_claude_dir / "claude"
    encoded = json.dumps(payload)
    fake_script.write_text(
        f"#!{sys.executable}\nimport sys\nsys.stdout.write({encoded!r})\nsys.exit(0)\n",
        encoding="utf-8",
    )
    fake_script.chmod(0o755)
    monkeypatch.setenv("PATH", f"{fake_claude_dir}:/usr/bin:/bin")


def test_run_step_flags_is_error(tmp_path, monkeypatch) -> None:
    """`is_error: true` in claude's JSON should flip `ok` to False."""
    _write_fake_claude_emitting(
        {
            "type": "result",
            "subtype": "error_during_execution",
            "is_error": True,
            "result": "Something broke mid-run.",
            "total_cost_usd": 0.01,
        },
        tmp_path,
        monkeypatch,
    )
    cwd = tmp_path / "repo"
    cwd.mkdir()
    result = run_step("anything", cwd=cwd, step_id=1)
    assert not result.ok
    assert result.summary == "Something broke mid-run."


def test_build_argv_includes_bypass_when_no_default_mode(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(claude_env, "read_default_permission_mode", lambda **_kw: None)
    argv = _build_claude_argv("hi", cwd=tmp_path)
    assert "--permission-mode" in argv
    idx = argv.index("--permission-mode")
    assert argv[idx + 1] == "bypassPermissions"
    assert argv[0] == "claude"


def test_build_argv_omits_bypass_when_auto_mode(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(claude_env, "read_default_permission_mode", lambda **_kw: "auto")
    argv = _build_claude_argv("hi", cwd=tmp_path)
    assert "--permission-mode" not in argv
    assert argv[0] == "claude"


def test_build_argv_wraps_with_runuser_when_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(claude_env.os, "geteuid", lambda: 0)
    monkeypatch.setattr(claude_env, "read_default_permission_mode", lambda **_kw: None)
    monkeypatch.setattr(claude_env, "ensure_autoclaude_user", lambda *_a, **_kw: None)
    monkeypatch.setattr(claude_env, "share_claude_config", lambda *_a, **_kw: None)
    monkeypatch.setattr(claude_env, "share_repo", lambda *_a, **_kw: None)
    monkeypatch.setattr(claude_env.shutil, "which", lambda name: "/usr/bin/runuser" if name == "runuser" else None)

    argv = _build_claude_argv("hi", cwd=tmp_path)
    assert argv[:5] == ["runuser", "-u", "autoclaude", "--preserve-environment", "--"]
    assert "claude" in argv
    assert "--permission-mode" in argv


def test_build_argv_does_not_wrap_when_root_in_auto_mode(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Root + defaultMode=auto: claude is fine without bypassPermissions, so do not provision the autoclaude user."""
    monkeypatch.setattr(claude_env.os, "geteuid", lambda: 0)
    monkeypatch.setattr(claude_env, "read_default_permission_mode", lambda **_kw: "auto")

    def _boom(*_a: object, **_kw: object) -> None:
        raise AssertionError("ensure_autoclaude_user must not be called in auto mode")

    monkeypatch.setattr(claude_env, "ensure_autoclaude_user", _boom)
    monkeypatch.setattr(claude_env, "share_claude_config", _boom)
    monkeypatch.setattr(claude_env, "share_repo", _boom)

    argv = _build_claude_argv("hi", cwd=tmp_path)
    assert argv[0] == "claude"
    assert "runuser" not in argv
    assert "sudo" not in argv
    assert "--permission-mode" not in argv


def test_run_step_returns_failed_result_when_user_creation_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(claude_env.os, "geteuid", lambda: 0)

    def _boom(*_a: object, **_kw: object) -> None:
        raise UserCreationError("autoclaude user cannot be created. Open an issue.")

    monkeypatch.setattr(claude_proc, "_build_claude_argv", _boom)
    cwd = tmp_path / "repo"
    cwd.mkdir()
    result = run_step("anything", cwd=cwd, step_id=1)
    assert not result.ok
    assert "issue" in result.stderr.lower()
    assert "issue" in result.summary.lower()


def test_run_step_flags_bail_marker(tmp_path, monkeypatch) -> None:
    """`[autoclaude:fail] ...` marker in result text should flip `ok` to False."""
    _write_fake_claude_emitting(
        {
            "type": "result",
            "subtype": "success",
            "is_error": False,
            "result": ("The `gh` CLI cannot operate on this repository.\n\n[autoclaude:fail] No GitHub remote configured"),
            "total_cost_usd": 0.01,
        },
        tmp_path,
        monkeypatch,
    )
    cwd = tmp_path / "repo"
    cwd.mkdir()
    result = run_step("anything", cwd=cwd, step_id=1)
    assert not result.ok
    assert result.fail_reason == "No GitHub remote configured"
    assert result.summary == "No GitHub remote configured"
