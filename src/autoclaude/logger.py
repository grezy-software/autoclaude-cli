"""Root logger setup for autoclaude-cli.

Two handlers are installed at import time and reused across the CLI:

- ``RichHandler`` on stdout -- visually identical to the previous
  ``console.print`` output (Rich markup is rendered the same way).
- ``RotatingFileHandler`` at ``~/.config/autoclaude/logs/autoclaude.log``
  -- always captures the full stream, independent of any upload. This is
  the offline fallback when the backend is unreachable or the process
  crashes before a batch can be sent.

A third ``BackendLogHandler`` is attached dynamically during a tick by
``TickLogger`` (see ``tick_logger.py``).
"""

from __future__ import annotations

import logging
from contextlib import contextmanager
from contextvars import ContextVar
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import TYPE_CHECKING

from rich.logging import RichHandler

from autoclaude.config import config_dir

if TYPE_CHECKING:
    from collections.abc import Iterator

LOGGER_NAME = "autoclaude"
LOG_FILE_MAX_BYTES = 10 * 1024 * 1024  # 10 MB
LOG_FILE_BACKUP_COUNT = 5

_initialized = False

# Active profile name for the current call chain. Set by multi-profile
# loops (tick, daemon, scheduler) before running a profile's work so
# every log line emitted from that section is tagged with the profile
# without callers having to thread a tag through every message.
_current_profile: ContextVar[str] = ContextVar("autoclaude_current_profile", default="-")


def set_current_profile(name: str) -> None:
    """Set the profile tag for log lines emitted on this call chain."""
    _current_profile.set(name or "-")


@contextmanager
def profile_context(name: str) -> Iterator[None]:
    """Scope :func:`set_current_profile` to a ``with`` block."""
    token = _current_profile.set(name or "-")
    try:
        yield
    finally:
        _current_profile.reset(token)


class _ProfileFilter(logging.Filter):
    """Inject ``record.profile`` from the active context var."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.profile = _current_profile.get()
        return True


def log_dir() -> Path:
    """Return the local directory where CLI logs are written."""
    return config_dir() / "logs"


def log_file_path() -> Path:
    return log_dir() / "autoclaude.log"


def _ensure_log_dir() -> Path:
    directory = log_dir()
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def _build_rich_handler() -> RichHandler:
    handler = RichHandler(
        rich_tracebacks=True,
        markup=True,
        show_time=True,
        omit_repeated_times=False,
        show_path=False,
        show_level=True,
        log_time_format="[%Y-%m-%d %H:%M:%S]",
    )
    handler.setLevel(logging.INFO)
    # RichHandler renders time + level itself; the formatter only controls
    # the trailing message column, so we prepend the profile here so it
    # lands between the time/level columns and the message body. The
    # leading ``\\[`` escapes Rich's markup parser -- a bare ``[name]``
    # would be swallowed as a style tag (e.g. ``[-]`` or ``[local]``
    # disappear when ``markup=True``), so we emit a literal ``\\[`` that
    # Rich renders as ``[``.
    handler.setFormatter(logging.Formatter(fmt="\\[%(profile)s] %(message)s"))
    handler.addFilter(_ProfileFilter())
    return handler


def _build_file_handler() -> RotatingFileHandler:
    _ensure_log_dir()
    handler = RotatingFileHandler(
        log_file_path(),
        maxBytes=LOG_FILE_MAX_BYTES,
        backupCount=LOG_FILE_BACKUP_COUNT,
        encoding="utf-8",
    )
    handler.setLevel(logging.DEBUG)
    formatter = logging.Formatter(
        fmt="%(asctime)s [%(profile)s] %(levelname)s [%(name)s] %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S%z",
    )
    handler.setFormatter(formatter)
    handler.addFilter(_ProfileFilter())
    return handler


def _init_logger() -> logging.Logger:
    global _initialized  # noqa: PLW0603
    logger = logging.getLogger(LOGGER_NAME)
    if _initialized:
        return logger
    logger.setLevel(logging.INFO)
    logger.propagate = False
    logger.addHandler(_build_rich_handler())
    logger.addHandler(_build_file_handler())
    _initialized = True
    return logger


def get_logger(name: str | None = None) -> logging.Logger:
    """Return a logger under the ``autoclaude`` namespace.

    Idempotent: handlers are installed once, on first call. Subsequent
    calls return child loggers that inherit the same handlers.
    """
    root = _init_logger()
    if name is None or name == LOGGER_NAME:
        return root
    return logging.getLogger(f"{LOGGER_NAME}.{name}")


__all__ = [
    "LOGGER_NAME",
    "get_logger",
    "log_dir",
    "log_file_path",
    "profile_context",
    "set_current_profile",
]
