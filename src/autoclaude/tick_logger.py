"""Attach the backend log uploader to a tick and guarantee crash-flush.

``TickLogger`` is the CLI's bridge between the stdlib ``logging`` module
and the backend. It is intentionally narrow:

- On entry it installs a ``BackendLogHandler`` on the root ``autoclaude``
  logger and emits a one-shot ``system`` record capturing the runtime
  environment (OS, Python version, CLI version, git SHA, etc.).
- While active, it owns ``sys.excepthook`` and handlers for SIGINT /
  SIGTERM. Any of these paths flushes the uploader synchronously before
  the process dies so the crash story reaches the backend.
- On exit it detaches the handler, flushes, closes the uploader, and
  restores the previous excepthook and signal handlers.

The uploader itself (see ``log_uploader.py``) owns the queue, worker
thread, and sidecar spill file.
"""

from __future__ import annotations

import contextlib
import logging
import os
import platform
import shutil
import signal
import subprocess
import sys
import traceback
from types import FrameType, TracebackType
from typing import TYPE_CHECKING, Any, Self

from autoclaude import __version__
from autoclaude.log_uploader import BackendLogHandler, BackendLogUploader
from autoclaude.logger import LOGGER_NAME, get_logger

if TYPE_CHECKING:
    from pathlib import Path

    from autoclaude.api_client import ApiClient


_CRASH_FLUSH_TIMEOUT = 5.0


class TickLogger:
    """Install + tear down the backend log handler for one tick."""

    def __init__(self, api_client: ApiClient, tick_id: int, *, repo_checkout: Path | None = None) -> None:
        self._api = api_client
        self._tick_id = tick_id
        self._repo = repo_checkout
        self._uploader: BackendLogUploader | None = None
        self._handler: BackendLogHandler | None = None
        self._root = logging.getLogger(LOGGER_NAME)
        self._prev_excepthook: Any = None
        self._prev_sigint: Any = None
        self._prev_sigterm: Any = None

    def __enter__(self) -> Self:
        self._uploader = BackendLogUploader(self._api, self._tick_id)
        self._handler = BackendLogHandler(self._uploader)
        self._root.addHandler(self._handler)
        self._install_hooks()
        self._emit_system_context()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        try:
            if exc is not None:
                self._emit_traceback(exc_type, exc, tb)
        finally:
            self._restore_hooks()
            if self._handler is not None:
                self._root.removeHandler(self._handler)
            if self._uploader is not None:
                self._uploader.close(timeout=_CRASH_FLUSH_TIMEOUT)

    # -- helpers ---------------------------------------------------------

    def _install_hooks(self) -> None:
        self._prev_excepthook = sys.excepthook
        sys.excepthook = self._on_unhandled_exception
        try:
            self._prev_sigint = signal.signal(signal.SIGINT, self._on_signal)
            self._prev_sigterm = signal.signal(signal.SIGTERM, self._on_signal)
        except ValueError:
            # `signal` only works on the main thread; skip quietly otherwise.
            self._prev_sigint = None
            self._prev_sigterm = None

    def _restore_hooks(self) -> None:
        if self._prev_excepthook is not None:
            sys.excepthook = self._prev_excepthook
            self._prev_excepthook = None
        if self._prev_sigint is not None:
            with contextlib.suppress(ValueError):
                signal.signal(signal.SIGINT, self._prev_sigint)
            self._prev_sigint = None
        if self._prev_sigterm is not None:
            with contextlib.suppress(ValueError):
                signal.signal(signal.SIGTERM, self._prev_sigterm)
            self._prev_sigterm = None

    def _on_unhandled_exception(
        self,
        exc_type: type[BaseException],
        exc: BaseException,
        tb: TracebackType | None,
    ) -> None:
        self._emit_traceback(exc_type, exc, tb)
        if self._uploader is not None:
            self._uploader.flush(timeout=_CRASH_FLUSH_TIMEOUT)
        if self._prev_excepthook is not None:
            self._prev_excepthook(exc_type, exc, tb)
        else:
            sys.__excepthook__(exc_type, exc, tb)

    def _on_signal(self, signum: int, _frame: FrameType | None) -> None:
        log = get_logger("tick")
        log.error("received signal %s; flushing logs before exit", signum, extra={"source": "cli"})
        if self._uploader is not None:
            self._uploader.flush(timeout=_CRASH_FLUSH_TIMEOUT)
        # Re-raise as KeyboardInterrupt for SIGINT, SystemExit otherwise so
        # normal __exit__ cleanup still runs.
        if signum == signal.SIGINT:
            raise KeyboardInterrupt
        raise SystemExit(128 + signum)

    def _emit_traceback(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        log = get_logger("tick")
        if exc_type is None or exc is None:
            return
        formatted = "".join(traceback.format_exception(exc_type, exc, tb))
        log.error(
            "unhandled %s: %s",
            exc_type.__name__,
            exc,
            extra={"source": "traceback", "payload": {"traceback": formatted}},
        )

    def _emit_system_context(self) -> None:
        log = get_logger("tick")
        log.info(
            "runner context: %s %s on %s/%s",
            "autoclaude-cli",
            __version__,
            platform.system(),
            platform.release(),
            extra={
                "source": "system",
                "payload": _collect_system_payload(self._repo),
            },
        )


def _collect_system_payload(repo_checkout: Path | None) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "cli_version": __version__,
        "python_version": platform.python_version(),
        "os": platform.system(),
        "os_release": platform.release(),
        "machine": platform.machine(),
    }
    if repo_checkout is not None:
        payload["repo_checkout"] = str(repo_checkout)
        payload["git_sha"] = _git_sha(repo_checkout)
    payload["claude_version"] = _claude_version()
    return payload


def _git_sha(repo: Path) -> str:
    if shutil.which("git") is None:
        return ""
    try:
        out = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(repo),
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
            env={**os.environ, "GIT_OPTIONAL_LOCKS": "0"},
        )
    except (subprocess.SubprocessError, OSError):
        return ""
    return out.stdout.strip() if out.returncode == 0 else ""


def _claude_version() -> str:
    if shutil.which("claude") is None:
        return ""
    try:
        out = subprocess.run(
            ["claude", "--version"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (subprocess.SubprocessError, OSError):
        return ""
    return out.stdout.strip() if out.returncode == 0 else ""


__all__ = ["TickLogger"]
