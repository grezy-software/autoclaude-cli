"""Thin wrapper around the ``gh`` CLI plus a preflight install check.

The runner requires the GitHub CLI to be installed and logged in:

- Clones and fetches against ``github.com`` authenticate via the token
  that ``gh`` exposes to ``git`` (see ``gh auth setup-git``), so the
  regular ``git`` subprocess wrappers pick up credentials without us
  plumbing tokens manually.
- The "backfill a repo on the user's account on first tick" flow creates
  the repo via ``gh repo create``; that cannot work without ``gh``.

We enforce the dependency at two layers:

1. ``diag`` surfaces the state to the user.
2. ``ensure_installed`` runs at tick start, so missing ``gh`` aborts the
   tick with a clear error instead of failing deep inside a subprocess.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path


class GhError(RuntimeError):
    """Raised when the ``gh`` CLI is missing, unauthenticated, or a command fails."""


def is_installed() -> bool:
    """Return whether the ``gh`` binary is resolvable on ``PATH``."""
    return shutil.which("gh") is not None


def ensure_installed() -> None:
    """Raise ``GhError`` if ``gh`` is not available on ``PATH``.

    Called at tick startup so the user gets a single actionable error
    rather than an opaque failure when ``git`` tries to hit ``github.com``
    without credentials.
    """
    if not is_installed():
        msg = (
            "GitHub CLI not found. Install it from https://cli.github.com and run "
            "`gh auth login` + `gh auth setup-git` so git can talk to github.com."
        )
        raise GhError(msg)


def is_authenticated() -> bool:
    """Return whether ``gh auth status`` reports a logged-in session.

    Returns ``False`` when ``gh`` itself is missing rather than raising,
    so callers can combine this with ``is_installed`` to drive layered UI.
    """
    if not is_installed():
        return False
    result = subprocess.run(
        ["gh", "auth", "status"],
        capture_output=True,
        text=True,
        check=False,
    )
    return result.returncode == 0


def ensure_authenticated() -> None:
    """Raise ``GhError`` if ``gh`` is missing or ``gh auth status`` fails."""
    ensure_installed()
    if not is_authenticated():
        msg = "gh CLI is installed but not logged in. Run `gh auth login` first."
        raise GhError(msg)


def gh(args: list[str], *, cwd: Path | None = None, check: bool = True) -> subprocess.CompletedProcess[str]:
    """Run ``gh`` and mirror ``_git``'s capture + error-mapping conventions.

    Ensures ``gh`` is installed first so the subprocess layer never sees a
    ``FileNotFoundError``.
    """
    ensure_installed()
    result = subprocess.run(
        ["gh", *args],
        cwd=str(cwd) if cwd else None,
        capture_output=True,
        text=True,
        check=False,
    )
    if check and result.returncode != 0:
        stderr = result.stderr.strip() or result.stdout.strip()
        msg = f"gh {' '.join(args)} failed ({result.returncode}): {stderr}"
        raise GhError(msg)
    return result


__all__ = [
    "GhError",
    "ensure_authenticated",
    "ensure_installed",
    "gh",
    "is_authenticated",
    "is_installed",
]
