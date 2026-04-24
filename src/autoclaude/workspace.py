"""Dedicated autoclaude workspace: clones + per-tick git worktrees.

The CLI must never run ``claude`` directly in the user's working checkout.
Instead, every source repo is mirrored into
``$AUTOCLAUDE_HOME/repos/<slug>/`` (a real clone, not bare) and each tick
spawns a short-lived worktree at ``$AUTOCLAUDE_HOME/worktrees/<slug>/<tick_id>/``
on its own ``autoclaude/<slug>/tick-<tick_id>`` branch. This keeps the
user's tree untouched and prevents branch collisions.

``$AUTOCLAUDE_HOME`` defaults to ``~/.autoclaude``. Override via env var
for tests or a non-default install.
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from autoclaude.logger import get_logger

_log = get_logger("workspace")

AUTOCLAUDE_HOME_ENV = "AUTOCLAUDE_HOME"
DEFAULT_HOME_DIRNAME = ".autoclaude"
REPOS_DIRNAME = "repos"
WORKTREES_DIRNAME = "worktrees"

_SLUG_SAFE_RE = re.compile(r"[^a-z0-9._-]+")
_SLUG_MAX_LENGTH = 48


def workspace_home() -> Path:
    """Return the autoclaude workspace root (``~/.autoclaude`` by default)."""
    override = os.environ.get(AUTOCLAUDE_HOME_ENV, "").strip()
    if override:
        return Path(override).expanduser()
    return Path.home() / DEFAULT_HOME_DIRNAME


def derive_slug(source: Path) -> str:
    """Build a filesystem-safe slug from a local repo path.

    Combines the directory name with a short hash of the absolute path so
    two repos with the same basename (e.g. two clones of ``nango``) never
    collide in ``repos/``.
    """
    resolved = source.resolve()
    base = _SLUG_SAFE_RE.sub("-", resolved.name.lower()).strip("-") or "repo"
    digest = hashlib.sha1(str(resolved).encode("utf-8"), usedforsecurity=False).hexdigest()[:8]
    return f"{base[:_SLUG_MAX_LENGTH]}-{digest}"


@dataclass(frozen=True)
class Worktree:
    path: Path
    branch: str


class WorkspaceError(RuntimeError):
    """Raised when clone/fetch/worktree operations fail."""


class Workspace:
    """Manage the per-source clone and its per-tick worktrees."""

    def __init__(self, home: Path, slug: str) -> None:
        self._home = home
        self._slug = slug

    @classmethod
    def for_source(cls, source: Path, *, home: Path | None = None) -> Workspace:
        return cls(home or workspace_home(), derive_slug(source))

    @property
    def home(self) -> Path:
        return self._home

    @property
    def slug(self) -> str:
        return self._slug

    @property
    def clone_path(self) -> Path:
        return self._home / REPOS_DIRNAME / self._slug

    @property
    def worktrees_root(self) -> Path:
        return self._home / WORKTREES_DIRNAME / self._slug

    def worktree_path(self, tick_id: int) -> Path:
        return self.worktrees_root / str(tick_id)

    def branch_name(self, tick_id: int) -> str:
        return f"autoclaude/{self._slug}/tick-{tick_id}"

    # --- sync -----------------------------------------------------------------

    def sync(self, source: Path) -> Path:
        """Ensure the clone exists and is up to date with ``source``.

        First call clones ``source`` into ``clone_path``; subsequent calls
        fetch every ref from it. Returns the clone path. The source is
        treated as the authoritative remote named ``origin``.
        """
        source_resolved = source.resolve()
        if not (source_resolved / ".git").exists():
            msg = f"source is not a git repo: {source_resolved}"
            raise WorkspaceError(msg)
        self.clone_path.parent.mkdir(parents=True, exist_ok=True)
        if not self.clone_path.exists():
            _git(["clone", str(source_resolved), str(self.clone_path)])
            _log.info("cloned %s -> %s", source_resolved, self.clone_path, extra={"source": "workspace"})
            return self.clone_path
        # Re-point origin in case the user moved the source checkout.
        _git(["remote", "set-url", "origin", str(source_resolved)], cwd=self.clone_path)
        _git(["fetch", "--prune", "origin"], cwd=self.clone_path)
        return self.clone_path

    # --- worktrees ------------------------------------------------------------

    def create_worktree(self, tick_id: int, *, base: str = "HEAD") -> Worktree:
        """Create a worktree + branch for this tick.

        ``base`` is the ref the branch forks from; defaults to the clone's
        current ``HEAD``. The worktree directory must not already exist --
        stale ones are pruned by ``remove_worktree`` on the previous tick
        or by the caller's ``finally`` block.
        """
        target = self.worktree_path(tick_id)
        branch = self.branch_name(tick_id)
        target.parent.mkdir(parents=True, exist_ok=True)
        # Prune any stale worktree metadata before re-using the path. Safe no-op
        # if nothing was registered.
        _git(["worktree", "prune"], cwd=self.clone_path, check=False)
        if target.exists():
            shutil.rmtree(target, ignore_errors=True)
        _git(
            ["worktree", "add", "-b", branch, str(target), base],
            cwd=self.clone_path,
        )
        _log.info("worktree %s on %s", target, branch, extra={"source": "workspace"})
        return Worktree(path=target, branch=branch)

    def remove_worktree(self, worktree: Worktree) -> None:
        """Remove the worktree directory but keep its branch.

        Branches outlive their worktree so the changes remain discoverable
        via ``git branch --list autoclaude/*`` even after cleanup.
        """
        if not worktree.path.exists():
            return
        _git(
            ["worktree", "remove", "--force", str(worktree.path)],
            cwd=self.clone_path,
            check=False,
        )
        if worktree.path.exists():
            shutil.rmtree(worktree.path, ignore_errors=True)
        _git(["worktree", "prune"], cwd=self.clone_path, check=False)


def _git(args: list[str], *, cwd: Path | None = None, check: bool = True) -> subprocess.CompletedProcess[str]:
    """Run ``git`` and raise ``WorkspaceError`` on non-zero exit.

    Centralised so every git call gets the same text/capture treatment and
    the same error-to-exception mapping.
    """
    result = subprocess.run(
        ["git", *args],
        cwd=str(cwd) if cwd else None,
        capture_output=True,
        text=True,
        check=False,
    )
    if check and result.returncode != 0:
        msg = f"git {' '.join(args)} failed ({result.returncode}): {result.stderr.strip() or result.stdout.strip()}"
        raise WorkspaceError(msg)
    return result


__all__ = [
    "AUTOCLAUDE_HOME_ENV",
    "DEFAULT_HOME_DIRNAME",
    "REPOS_DIRNAME",
    "WORKTREES_DIRNAME",
    "Workspace",
    "WorkspaceError",
    "Worktree",
    "derive_slug",
    "workspace_home",
]
