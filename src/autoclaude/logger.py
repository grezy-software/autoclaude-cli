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
from logging.handlers import RotatingFileHandler
from pathlib import Path

from rich.logging import RichHandler

from autoclaude.config import config_dir

LOGGER_NAME = "autoclaude"
LOG_FILE_MAX_BYTES = 10 * 1024 * 1024  # 10 MB
LOG_FILE_BACKUP_COUNT = 5

_initialized = False


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
        show_time=False,
        show_path=False,
        show_level=True,
    )
    handler.setLevel(logging.INFO)
    return handler


def _build_file_handler() -> RotatingFileHandler:
    _ensure_log_dir()
    handler = RotatingFileHandler(
        log_file_path(),
        maxBytes=LOG_FILE_MAX_BYTES,
        backupCount=LOG_FILE_BACKUP_COUNT,
        encoding="utf-8",
    )
    handler.setLevel(logging.INFO)
    formatter = logging.Formatter(
        fmt="%(asctime)s %(levelname)s [%(name)s] %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S%z",
    )
    handler.setFormatter(formatter)
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
]
