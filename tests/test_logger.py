"""Regression tests for the logging configuration."""

from __future__ import annotations

import logging

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
