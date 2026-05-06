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


def current_user_login() -> str:
    """Return the GitHub login of the authenticated ``gh`` user.

    Used by the auto-create flow to choose the owner of a freshly-created
    project repo. Raises ``GhError`` when ``gh`` is missing/unauthenticated
    or the API response cannot be parsed.
    """
    result = gh(["api", "user", "--jq", ".login"])
    login = result.stdout.strip()
    if not login:
        msg = "gh api user returned an empty login"
        raise GhError(msg)
    return login


def repo_exists(repo: str) -> bool:
    """Return True when ``gh repo view <owner/name>`` resolves the repo.

    The auto-create flow polls this with incrementing suffixes to find an
    available name. Any non-zero exit (404, network) is treated as
    "doesn't exist for our purposes" so we err on the side of creating
    rather than failing the tick on a transient lookup glitch.
    """
    result = gh(
        ["repo", "view", repo, "--json", "name"],
        check=False,
    )
    return result.returncode == 0


def pr_create(
    *,
    base: str,
    head: str,
    cwd: Path,
    title: str | None = None,
    body: str | None = None,
) -> str:
    """Open a pull request from ``head`` into ``base`` and return its URL.

    Uses ``--fill`` (commit subject/body) when no explicit title is given.
    Run from inside the repo checkout so ``gh`` resolves the correct repo.
    """
    args = ["pr", "create", "--base", base, "--head", head]
    if title:
        args += ["--title", title, "--body", body or ""]
    else:
        args.append("--fill")
    result = gh(args, cwd=cwd)
    return result.stdout.strip()


def pr_url_for_branch(*, head: str, cwd: Path) -> str | None:
    """Return the URL of the open PR whose head ref is ``head``, or None.

    Used to recover the PR URL when ``pr_create`` fails because a PR is
    already open for the branch (the agent may have opened it itself).
    """
    result = gh(
        ["pr", "view", head, "--json", "url", "--jq", ".url"],
        cwd=cwd,
        check=False,
    )
    if result.returncode != 0:
        return None
    url = result.stdout.strip()
    return url or None


def pr_merge(
    *,
    pr_url: str,
    cwd: Path,
    method: str = "squash",
    delete_branch: bool = True,
) -> None:
    """Merge ``pr_url`` and optionally delete the head branch.

    ``method`` picks the merge strategy flag (``squash``/``merge``/``rebase``).
    Run from inside the repo checkout so ``gh`` resolves the correct repo.
    """
    args = ["pr", "merge", pr_url, f"--{method}"]
    if delete_branch:
        args.append("--delete-branch")
    gh(args, cwd=cwd)


def repo_create(repo: str, *, private: bool = True) -> None:
    """Create ``<owner>/<name>`` on GitHub via ``gh repo create``.

    ``--private`` is the default; pass ``private=False`` only when the
    project's policy explicitly opts into a public repo. ``--add-readme``
    seeds an initial commit so the remote's default branch (``main``) exists
    immediately; without it, ``git worktree add ... origin/main`` on the
    very first tick fails with ``invalid reference: origin/main`` because
    the remote has no commits yet.
    """
    visibility = "--private" if private else "--public"
    gh(["repo", "create", repo, visibility, "--add-readme"])


__all__ = [
    "GhError",
    "current_user_login",
    "ensure_authenticated",
    "ensure_installed",
    "gh",
    "is_authenticated",
    "is_installed",
    "pr_create",
    "pr_merge",
    "pr_url_for_branch",
    "repo_create",
    "repo_exists",
]
