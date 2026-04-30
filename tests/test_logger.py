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
