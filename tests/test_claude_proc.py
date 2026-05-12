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
    _extract_token_total,
    _format_argv_for_log,
    run_step,
)


@pytest.fixture(autouse=True)
def _isolate_claude_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Default to a non-root, no-settings environment so existing tests stay deterministic."""
    claude_env.reset_caches()
    monkeypatch.setattr(claude_env.os, "geteuid", lambda: 1000)


def test_extract_token_total_sums_nested_usage_tokens() -> None:
    parsed = {
        "session_id": "abc",
        "total_cost_usd": 0.5,
        "usage": {
            "input_tokens": 100,
            "output_tokens": 50,
            "cache_creation_input_tokens": 20,
            "cache_read_input_tokens": 10,
        },
    }
    assert _extract_token_total(parsed) == 180


def test_extract_token_total_sums_top_level_usage_tokens() -> None:
    parsed = {"session_id": "abc", "input_tokens": 5, "output_tokens": 3}
    assert _extract_token_total(parsed) == 8


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
        'print(json.dumps({"type": "system", "subtype": "init", "session_id": "sess-1", "model": "opus"}))\n'
        'print(json.dumps({"type": "result", "is_error": False, "session_id": "sess-1", "total_cost_usd": 0.42, "result": "done"}))\n',
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
        f"#!{sys.executable}\nimport sys\nsys.stdout.write({encoded!r} + '\\n')\nsys.exit(0)\n",
        encoding="utf-8",
    )
    fake_script.chmod(0o755)
    monkeypatch.setenv("PATH", f"{fake_claude_dir}:/usr/bin:/bin")


def test_format_argv_elides_prompt_and_keeps_wrapper_visible() -> None:
    argv = [
        "runuser",
        "-u",
        "autoclaude",
        "--preserve-environment",
        "--",
        "claude",
        "-p",
        "this is a very long prompt body that we do not want flooding the log",
        "--output-format",
        "stream-json",
        "--model",
        "opus",
    ]
    rendered = _format_argv_for_log(argv)
    # Wrapper details must remain visible so the operator can paste the line.
    assert "runuser -u autoclaude --preserve-environment -- claude" in rendered
    assert "--output-format stream-json" in rendered
    assert "--model opus" in rendered
    # The actual prompt body must NOT appear; an elision marker takes its place.
    assert "long prompt body" not in rendered
    assert "chars elided" in rendered


def test_format_argv_quotes_args_with_spaces() -> None:
    rendered = _format_argv_for_log(["claude", "--cwd", "/path with spaces"])
    assert "'/path with spaces'" in rendered


def test_run_step_logs_command_when_subprocess_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """When claude exits non-zero, the redacted argv must be logged at ERROR for diagnosis."""
    fake_dir = tmp_path / "bin"
    fake_dir.mkdir()
    fake = fake_dir / "claude"
    fake.write_text(f"#!{sys.executable}\nimport sys\nsys.exit(1)\n", encoding="utf-8")
    fake.chmod(0o755)
    monkeypatch.setenv("PATH", f"{fake_dir}:/usr/bin:/bin")
    cwd = tmp_path / "repo"
    cwd.mkdir()

    records: list[logging.LogRecord] = []

    class _Capture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    claude_logger = logging.getLogger("autoclaude.claude")
    capture = _Capture(level=logging.DEBUG)
    claude_logger.addHandler(capture)
    try:
        result = run_step("anything", cwd=cwd, step_id=99)
    finally:
        claude_logger.removeHandler(capture)

    assert not result.ok
    exec_errors = [r for r in records if getattr(r, "source", None) == "claude_exec" and r.levelno >= logging.ERROR]
    assert exec_errors, "expected an ERROR log line containing the failed command"
    message = exec_errors[0].getMessage()
    assert "claude" in message
    assert "rc=1" in message


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
    argv, _env = _build_claude_argv("hi", cwd=tmp_path)
    assert "--permission-mode" in argv
    idx = argv.index("--permission-mode")
    assert argv[idx + 1] == "bypassPermissions"
    assert argv[0] == "claude"


def test_build_argv_omits_bypass_when_auto_mode(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(claude_env, "read_default_permission_mode", lambda **_kw: "auto")
    argv, _env = _build_claude_argv("hi", cwd=tmp_path)
    assert "--permission-mode" not in argv
    assert argv[0] == "claude"


def test_build_argv_wraps_with_runuser_when_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Root + bypass + autoclaude user already provisioned: wrap with runuser, no install-time setup."""
    monkeypatch.setattr(claude_env.os, "geteuid", lambda: 0)
    monkeypatch.setattr(claude_env, "read_default_permission_mode", lambda **_kw: None)
    monkeypatch.setattr(claude_env, "autoclaude_user_exists", lambda: True)
    monkeypatch.setattr(claude_env, "share_repo", lambda *_a, **_kw: None)
    monkeypatch.setattr(claude_env, "share_claude_credentials", lambda *_a, **_kw: None)
    monkeypatch.setattr(
        claude_env,
        "autoclaude_subprocess_env_overrides",
        lambda *_a, **_kw: {"HOME": "/home/autoclaude"},
    )
    monkeypatch.setattr(claude_env.shutil, "which", lambda name: "/usr/bin/runuser" if name == "runuser" else None)

    # Install-time helpers must NOT run during a tick. Wire raisers as guards.
    def _must_not_run(*_a: object, **_kw: object) -> None:
        raise AssertionError("install-time helper called during tick")

    monkeypatch.setattr(claude_env, "ensure_autoclaude_user", _must_not_run)
    monkeypatch.setattr(claude_env, "share_claude_config", _must_not_run)
    monkeypatch.setattr(claude_env, "share_claude_binary", _must_not_run)

    argv, env_overrides = _build_claude_argv("hi", cwd=tmp_path)
    assert argv[:5] == ["runuser", "-u", "autoclaude", "--preserve-environment", "--"]
    assert "claude" in argv
    assert "--permission-mode" in argv
    # HOME must override the parent's, otherwise the wrapped claude locks the
    # same session files as the root parent and hangs in epoll_wait.
    assert env_overrides == {"HOME": "/home/autoclaude"}


def test_build_argv_returns_empty_env_when_not_wrapping(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Auto mode (no wrap) must NOT inject HOME -- only the autoclaude branch needs that."""
    monkeypatch.setattr(claude_env, "read_default_permission_mode", lambda **_kw: "auto")
    monkeypatch.setattr(claude_env, "autoclaude_user_exists", lambda: False)
    _argv, env_overrides = _build_claude_argv("hi", cwd=tmp_path)
    assert env_overrides == {}


def test_build_argv_raises_when_root_bypass_without_autoclaude_user(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Root + bypass + no autoclaude user is the one combination claude itself refuses.

    Surface a friendly remediation here pointing the operator at
    ``autoclaude init --user-autoclaude`` instead of letting claude fail
    with a buried stream-json error a few seconds later.
    """
    monkeypatch.setattr(claude_env.os, "geteuid", lambda: 0)
    monkeypatch.setattr(claude_env, "read_default_permission_mode", lambda **_kw: None)
    monkeypatch.setattr(claude_env, "autoclaude_user_exists", lambda: False)

    def _must_not_run(*_a: object, **_kw: object) -> None:
        raise AssertionError("autoclaude wrapper helper called when user missing")

    monkeypatch.setattr(claude_env, "ensure_autoclaude_user", _must_not_run)
    monkeypatch.setattr(claude_env, "share_claude_config", _must_not_run)
    monkeypatch.setattr(claude_env, "share_claude_binary", _must_not_run)
    monkeypatch.setattr(claude_env, "share_per_tick_for_autoclaude_user", _must_not_run)

    with pytest.raises(UserCreationError) as excinfo:
        _build_claude_argv("hi", cwd=tmp_path)
    message = str(excinfo.value)
    assert "autoclaude init --user-autoclaude" in message
    assert "bypassPermissions" in message


def test_build_argv_does_not_wrap_when_root_in_auto_mode_without_autoclaude_user(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Root + defaultMode=auto + no autoclaude user: no wrapping, no provisioning checks."""
    monkeypatch.setattr(claude_env.os, "geteuid", lambda: 0)
    monkeypatch.setattr(claude_env, "read_default_permission_mode", lambda **_kw: "auto")
    monkeypatch.setattr(claude_env, "autoclaude_user_exists", lambda: False)

    def _must_not_run(*_a: object, **_kw: object) -> None:
        raise AssertionError("autoclaude wrapper helper called when user missing")

    monkeypatch.setattr(claude_env, "share_per_tick_for_autoclaude_user", _must_not_run)
    monkeypatch.setattr(claude_env, "ensure_autoclaude_user", _must_not_run)
    monkeypatch.setattr(claude_env, "share_claude_config", _must_not_run)
    monkeypatch.setattr(claude_env, "share_claude_binary", _must_not_run)

    argv, _env = _build_claude_argv("hi", cwd=tmp_path)
    assert argv[0] == "claude"
    assert "runuser" not in argv
    assert "sudo" not in argv
    assert "--permission-mode" not in argv


def test_build_argv_wraps_when_user_exists_even_in_auto_mode(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Wrap with runuser whenever the autoclaude user exists, even in auto mode.

    The autoclaude user is the source of truth: ``init`` provisions it only
    when needed (or when forced), so its presence == intent to wrap.
    """
    monkeypatch.setattr(claude_env.os, "geteuid", lambda: 0)
    monkeypatch.setattr(claude_env, "read_default_permission_mode", lambda **_kw: "auto")
    monkeypatch.setattr(claude_env, "autoclaude_user_exists", lambda: True)
    monkeypatch.setattr(claude_env, "share_repo", lambda *_a, **_kw: None)
    monkeypatch.setattr(claude_env, "share_claude_credentials", lambda *_a, **_kw: None)
    monkeypatch.setattr(
        claude_env,
        "autoclaude_subprocess_env_overrides",
        lambda *_a, **_kw: {"HOME": "/home/autoclaude"},
    )
    monkeypatch.setattr(claude_env.shutil, "which", lambda name: "/usr/bin/runuser" if name == "runuser" else None)

    argv, env_overrides = _build_claude_argv("hi", cwd=tmp_path)
    assert argv[:5] == ["runuser", "-u", "autoclaude", "--preserve-environment", "--"]
    assert "--permission-mode" not in argv  # auto mode -> no bypass flag
    assert env_overrides == {"HOME": "/home/autoclaude"}


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


def _write_fake_claude_script(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, body: str) -> None:
    """Drop a fake `claude` on PATH whose Python body is ``body``.

    ``body`` runs in a fresh process, with ``sys`` and ``time`` pre-imported.
    Used by the watchdog tests to script post-result hangs and idle hangs.
    """
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    fake_dir = tmp_path / "bin"
    fake_dir.mkdir()
    fake = fake_dir / "claude"
    fake.write_text(
        f"#!{sys.executable}\nimport sys, time, json\n{body}\n",
        encoding="utf-8",
    )
    fake.chmod(0o755)
    monkeypatch.setenv("PATH", f"{fake_dir}:/usr/bin:/bin")


def test_run_step_kills_after_post_result_grace_when_subprocess_hangs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Reproduce the Bun event-loop hang and verify the post-result watchdog.

    Claude flushes the result then parks forever. The watchdog must terminate
    it within the grace window and still report ``ok=True`` because the work
    itself completed.
    """
    _write_fake_claude_script(
        tmp_path,
        monkeypatch,
        body=(
            "sys.stdout.write(json.dumps({"
            "'type':'result','is_error':False,"
            "'session_id':'sess-1','total_cost_usd':0.10,'result':'done'"
            "}) + '\\n')\n"
            "sys.stdout.flush()\n"
            "time.sleep(120)\n"  # would hang for 2 min if not killed
        ),
    )
    cwd = tmp_path / "repo"
    cwd.mkdir()

    result = run_step(
        "anything",
        cwd=cwd,
        step_id=1,
        post_result_grace=0.3,
        timeout=30,
    )

    assert result.duration_ms < 5_000, f"watchdog should have killed the hung subprocess; actual duration_ms={result.duration_ms}"
    assert result.ok is True, "post-result kill is internal cleanup, not a failure"
    assert result.session_id == "sess-1"
    assert any(e.get("type") == "result" for e in result.events)


def test_run_step_kills_after_idle_timeout_when_no_output(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify the idle-stdout watchdog kills hangs that occur before any output.

    ``ok`` is False because the work never completed.
    """
    _write_fake_claude_script(
        tmp_path,
        monkeypatch,
        body="time.sleep(120)\n",
    )
    cwd = tmp_path / "repo"
    cwd.mkdir()

    result = run_step(
        "anything",
        cwd=cwd,
        step_id=1,
        idle_timeout=0.5,
        timeout=30,
    )

    assert result.duration_ms < 5_000
    assert result.ok is False
    assert "stdout idle" in result.stderr


def test_run_step_default_idle_timeout_kills_pre_init_hang(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Reproduce the pre-init hang and verify the default idle_timeout fires.

    Regression guard: in 2.5.10 the idle_timeout default was ``None``, which
    let a claude subprocess that never emitted a ``system`` event hold the
    tick for the full 1h timeout. The default is now 300s; we override it for
    the test but rely on the same code path that production uses.
    """
    _write_fake_claude_script(
        tmp_path,
        monkeypatch,
        body="time.sleep(120)\n",
    )
    cwd = tmp_path / "repo"
    cwd.mkdir()

    # Use the production default code path: do not pass idle_timeout explicitly.
    # We can't wait 300s in a unit test, so we monkeypatch the module default.
    monkeypatch.setattr(claude_proc, "_DEFAULT_IDLE_TIMEOUT_SECS", 0.5)

    result = run_step("anything", cwd=cwd, step_id=1, timeout=30)

    assert result.duration_ms < 5_000
    assert result.ok is False
    assert "stdout idle" in result.stderr


def test_run_step_archives_subprocess_streams_to_log_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The raw stdout and stderr of the claude subprocess must be archived
    to a per-step file under ``logs/streams`` so an operator can replay
    exactly what the child wrote, with timestamps and stream tags.
    """  # noqa: D205
    # Redirect logs to tmp via XDG.
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    # Force config_dir to re-evaluate (it caches based on env).
    from autoclaude import config, logger  # noqa: PLC0415

    monkeypatch.setattr(config, "config_dir", lambda: tmp_path / "xdg" / "autoclaude")
    monkeypatch.setattr(logger, "log_dir", lambda: tmp_path / "xdg" / "autoclaude" / "logs")

    _write_fake_claude_script(
        tmp_path,
        monkeypatch,
        body=(
            "sys.stderr.write('warmup\\n')\n"
            "sys.stdout.write(json.dumps({"
            "'type':'system','subtype':'init','session_id':'sess-archive','model':'opus'"
            "}) + '\\n')\n"
            "sys.stdout.write(json.dumps({"
            "'type':'result','is_error':False,"
            "'session_id':'sess-archive','total_cost_usd':0.01,'result':'ok'"
            "}) + '\\n')\n"
        ),
    )
    cwd = tmp_path / "repo"
    cwd.mkdir()

    result = run_step("anything", cwd=cwd, step_id=42)
    assert result.ok is True

    streams = logger.streams_dir()
    archives = sorted(streams.glob("claude-stream-*-step-42.log"))
    assert len(archives) == 1, f"expected one archive, got {len(archives)}"
    body = archives[0].read_text(encoding="utf-8")
    # Header recorded so the file is self-describing.
    assert "step_id: 42" in body
    assert "argv:" in body
    # Both streams archived with their tag.
    assert "stdout: " in body
    assert "stderr: " in body
    # Substantive payload preserved verbatim.
    assert '"type": "system"' in body or "'type': 'system'" in body or "type" in body
    assert "warmup" in body
    assert "sess-archive" in body


def test_run_step_stream_archive_rotates_to_at_most_five(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Six successive run_step invocations leave at most STREAMS_BACKUP_COUNT
    files in the streams directory, with the newest ones kept.
    """  # noqa: D205
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    from autoclaude import config, logger  # noqa: PLC0415

    monkeypatch.setattr(config, "config_dir", lambda: tmp_path / "xdg" / "autoclaude")
    monkeypatch.setattr(logger, "log_dir", lambda: tmp_path / "xdg" / "autoclaude" / "logs")

    _write_fake_claude_emitting(
        {
            "type": "result",
            "is_error": False,
            "session_id": "rot",
            "total_cost_usd": 0.0,
            "result": "ok",
        },
        tmp_path,
        monkeypatch,
    )
    cwd = tmp_path / "repo"
    cwd.mkdir()

    # Pre-seed older files at distinct mtimes so rotation is deterministic.
    streams = logger.streams_dir()
    streams.mkdir(parents=True, exist_ok=True)
    import os  # noqa: PLC0415

    for i in range(6):
        f = streams / f"claude-stream-pre-{i:02d}.log"
        f.write_text(f"pre #{i}\n")
        ts = 1_700_000_000 + i
        os.utime(str(f), (ts, ts))

    result = run_step("anything", cwd=cwd, step_id=7)
    assert result.ok is True

    files = sorted(streams.glob("claude-stream-*.log"))
    assert len(files) == logger.STREAMS_BACKUP_COUNT, (
        f"expected exactly {logger.STREAMS_BACKUP_COUNT} files after rotation; got {len(files)}: {[p.name for p in files]}"
    )
    # The brand-new step-7 archive must be present.
    names = {p.name for p in files}
    assert any("step-7" in n for n in names), f"new archive missing: {names}"
    # The oldest pre-seeded file must have been pruned.
    assert "claude-stream-pre-00.log" not in names


def test_run_step_natural_exit_unaffected_by_watchdog(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify the watchdog is silent when the subprocess exits naturally.

    Historical ok/rc semantics must be preserved when no kill_reason fires.
    """
    _write_fake_claude_emitting(
        {
            "type": "result",
            "is_error": False,
            "session_id": "sess-9",
            "total_cost_usd": 0.05,
            "result": "ok",
        },
        tmp_path,
        monkeypatch,
    )
    cwd = tmp_path / "repo"
    cwd.mkdir()

    result = run_step(
        "anything",
        cwd=cwd,
        step_id=1,
        post_result_grace=0.5,
        idle_timeout=0.5,
        timeout=30,
    )

    assert result.ok is True
    assert result.session_id == "sess-9"
    assert "stdout idle" not in result.stderr
    assert "timed out" not in result.stderr
