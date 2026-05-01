"""Regression tests for the logging configuration."""

from __future__ import annotations

import logging
import time
from pathlib import Path

import pytest

from autoclaude import logger as autoclaude_logger


def test_logger_level_is_debug() -> None:
    """The autoclaude logger must accept DEBUG records.

    A regression here would silently drop the upstream `claude exec: ...`
    DEBUG line that we rely on to diagnose runuser/permission failures.
    """
    log = autoclaude_logger.get_logger()
    assert log.level == logging.DEBUG, (
        f"autoclaude logger level is {logging.getLevelName(log.level)}; "
        f"DEBUG records would be filtered before reaching the file handler."
    )


def test_file_handler_captures_debug() -> None:
    """At least one handler attached to the autoclaude logger must accept DEBUG.

    The console (Rich) handler stays at INFO so the terminal is not spammed; the
    file handler must capture DEBUG so post-mortems include upstream context.
    """
    log = autoclaude_logger.get_logger()
    debug_capable = [h for h in log.handlers if h.level <= logging.DEBUG]
    assert debug_capable, (
        "no handler on the autoclaude logger accepts DEBUG records; "
        "tick failure context will be invisible in the log file."
    )


def test_file_formatter_renders_local_time_not_utc() -> None:
    """The file handler must format timestamps in the system local zone.

    Operators read ``autoclaude.log`` to correlate with their wall clock; UTC
    would force them to do mental zone math on every line and would break
    grep/awk against ``date`` output.
    """
    import logging  # noqa: PLC0415
    import time  # noqa: PLC0415
    from logging.handlers import RotatingFileHandler  # noqa: PLC0415

    log = autoclaude_logger.get_logger()
    file_handlers = [h for h in log.handlers if isinstance(h, RotatingFileHandler)]
    assert file_handlers, "expected a RotatingFileHandler on the autoclaude logger"
    fmt = file_handlers[0].formatter
    assert fmt is not None

    record = logging.LogRecord(
        name="autoclaude.test",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="hi",
        args=None,
        exc_info=None,
    )
    record.created = time.time()
    record.profile = "-"
    rendered = fmt.format(record)

    # The local zone offset (e.g. ``+0200`` for CEST). If the formatter were
    # using gmtime, the offset would always be ``+0000``.
    expected_offset = time.strftime("%z", time.localtime(record.created))
    assert expected_offset in rendered, (
        f"file handler emitted {rendered!r} which does not contain the local "
        f"timezone offset {expected_offset!r}; the formatter is likely using "
        f"gmtime (UTC) instead of localtime."
    )


def test_streams_dir_is_under_log_dir() -> None:
    """Verify the streams directory lives next to autoclaude.log.

    A single archive setting (XDG_CONFIG_HOME) must cover both.
    """
    assert autoclaude_logger.streams_dir() == autoclaude_logger.log_dir() / "streams"


def test_allocate_stream_log_path_creates_dir_and_returns_unique_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify path is unique per call and the directory is auto-created.

    The path is allocated under streams_dir; back-to-back calls (>=1s apart)
    return distinct paths because the filename embeds the timestamp.
    """
    monkeypatch.setattr(autoclaude_logger, "log_dir", lambda: tmp_path / "logs")

    p1 = autoclaude_logger.allocate_stream_log_path(step_id=1)
    assert p1.parent == tmp_path / "logs" / "streams"
    assert p1.parent.is_dir()
    assert "step-1" in p1.name
    assert p1.name.startswith("claude-stream-")

    # Sleep one second so the second-resolution timestamp moves; otherwise the
    # test asserts a property that the implementation does not guarantee
    # (sub-second uniqueness). One file per second is more than enough for the
    # production use case (one per tick).
    time.sleep(1)
    p2 = autoclaude_logger.allocate_stream_log_path(step_id=2)
    assert p2 != p1


def test_allocate_stream_log_path_prunes_to_backup_count(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """After enough allocations, only ``STREAMS_BACKUP_COUNT`` files remain.

    Mirrors the production loop: caller writes to each path, then the next
    allocation prunes the oldest. We simulate it directly to keep the test fast.
    """
    monkeypatch.setattr(autoclaude_logger, "log_dir", lambda: tmp_path / "logs")
    streams = tmp_path / "logs" / "streams"
    streams.mkdir(parents=True)

    keep = autoclaude_logger.STREAMS_BACKUP_COUNT
    # Drop ``keep + 3`` pre-existing files with strictly increasing mtimes so
    # the prune order is deterministic.
    for i in range(keep + 3):
        f = streams / f"claude-stream-old-{i:02d}.log"
        f.write_text(f"old #{i}\n")
        # Force mtime spread so sort-by-mtime is well-defined.
        ts = 1_700_000_000 + i
        os_path = str(f)
        import os  # noqa: PLC0415

        os.utime(os_path, (ts, ts))

    # Allocating a new path leaves room for the new file: at most ``keep - 1``
    # pre-existing files survive.
    new_path = autoclaude_logger.allocate_stream_log_path(step_id=99)
    new_path.write_text("new\n")  # actually create it so the count check is correct

    remaining = sorted(streams.glob("claude-stream-*.log"))
    assert len(remaining) == keep, (
        f"expected {keep} files after allocation+write; got {len(remaining)}: "
        f"{[p.name for p in remaining]}"
    )
    # The newest pre-existing files are kept; the oldest ones are pruned.
    kept_names = {p.name for p in remaining}
    assert new_path.name in kept_names
    # The oldest pre-existing one (-00) must have been pruned.
    assert "claude-stream-old-00.log" not in kept_names


def test_console_handler_stays_at_info_or_above() -> None:
    """The Rich console handler should stay at INFO so DEBUG noise is hidden from the terminal."""
    from rich.logging import RichHandler  # noqa: PLC0415 (local import; tests-only dependency surface)

    log = autoclaude_logger.get_logger()
    rich_handlers = [h for h in log.handlers if isinstance(h, RichHandler)]
    assert rich_handlers, "expected a Rich console handler on the autoclaude logger"
    for h in rich_handlers:
        assert h.level >= logging.INFO, (
            f"Rich console handler is at {logging.getLevelName(h.level)}; "
            f"keeping it at INFO or above avoids flooding the terminal."
        )
