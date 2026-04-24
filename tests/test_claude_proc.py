"""Tests for the claude subprocess runner."""

from __future__ import annotations

import logging
import sys

import pytest

from autoclaude.claude_proc import _parse_result_metadata, run_step


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
    fake_script.write_text(
        f"#!{sys.executable}\n"
        "import json, sys\n"
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
