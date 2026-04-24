"""Dedicated autoclaude workspace: clones + per-tick git worktrees.

The CLI must never run ``claude`` directly in the user's working checkout.
Instead, every source repo is mirrored into
``$AUTOCLAUDE_HOME/repos/<slug>/`` (a real clone, not bare) and each tick
spawns a short-lived worktree at ``$AUTOCLAUDE_HOME/worktrees/<slug>/<tick_id>/``
on its own ``autoclaude/<slug>/tick-<tick_id>`` branch. This keeps the
user's tree untouched and prevents branch collisions.

``$AUTOCLAUDE_HOME`` defaults to ``~/.autoclaude``. Override via env var
for tests or a non-default install.

All git work is gated on the GitHub CLI (``gh``) being installed. The
actual ``git`` subprocesses are still what do the local mechanics
(``clone``, ``fetch``, ``worktree``), but pushing/fetching over the
network against ``github.com`` relies on ``gh``'s credential helper
being wired in, so we refuse to start without ``gh`` on ``PATH``.
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from autoclaude.gh import ensure_installed as ensure_gh_installed
from autoclaude.logger import get_logger

_log = get_logger("workspace")

AUTOCLAUDE_HOME_ENV = "AUTOCLAUDE_HOME"
DEFAULT_HOME_DIRNAME = ".autoclaude"
REPOS_DIRNAME = "repos"
WORKTREES_DIRNAME = "worktrees"
_GITHUB_REMOTE_NAME = "github"

_SLUG_SAFE_RE = re.compile(r"[^a-z0-9._-]+")
_SLUG_MAX_LENGTH = 48

# Owner/repo segments: letters, digits, dots, underscores, hyphens. Matches
# what GitHub accepts; deliberately loose because we validate by re-emitting
# a canonical URL, not by rejecting spelling variants.
_OWNER_REPO_RE = re.compile(r"^([A-Za-z0-9][A-Za-z0-9._-]*)/([A-Za-z0-9._-]+?)$")


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
        _require_gh()
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

    def configure_github_remote(self, github_repo: str) -> None:
        """Attach a ``github`` remote pointing at ``github.com/<owner>/<repo>.git``.

        ``origin`` stays pinned to the user's local source checkout so
        ``sync`` keeps working against the user's working copy. ``gh`` prefers
        any remote whose URL resolves to a GitHub host (in name priority
        ``upstream`` > ``github`` > ``origin``), so this gives ``gh issue
        list`` / ``gh pr create`` a real GitHub context without disturbing
        the local-path fetch flow.

        ``github_repo`` is accepted in any common form (``owner/repo``, a
        full HTTPS URL, or an SSH URL) and normalised to the canonical
        HTTPS clone URL. Malformed values (e.g. the server double-prefixed
        ``https://github.com/https://github.com/...``) are rejected rather
        than stored, since they break every later ``gh`` call.

        Idempotent: creates the remote on first call, ``set-url`` on
        subsequent calls. No-op on empty ``github_repo``.
        """
        if not github_repo:
            return
        try:
            url = _canonical_github_clone_url(github_repo)
        except ValueError as exc:
            raise WorkspaceError(f"invalid github_repo {github_repo!r}: {exc}") from exc
        existing = _git(["remote", "get-url", _GITHUB_REMOTE_NAME], cwd=self.clone_path, check=False)
        if existing.returncode == 0:
            if existing.stdout.strip() == url:
                return
            _git(["remote", "set-url", _GITHUB_REMOTE_NAME, url], cwd=self.clone_path)
            _log.info("updated github remote -> %s", url, extra={"source": "workspace"})
            return
        _git(["remote", "add", _GITHUB_REMOTE_NAME, url], cwd=self.clone_path)
        _log.info("added github remote %s -> %s", _GITHUB_REMOTE_NAME, url, extra={"source": "workspace"})

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


_URL_SCHEMES: tuple[str, ...] = (
    "https://",
    "http://",
    "git+https://",
    "ssh://",
    "git+ssh://",
)
_URL_HOSTS: tuple[str, ...] = ("github.com/", "www.github.com/")


def _strip_one_prefix(path: str) -> str:
    """Remove one recognised scheme / user-info / host prefix, returning the remainder.

    Returns ``path`` unchanged when nothing matches, which the caller uses as
    the loop-terminating fixed point.
    """
    lowered = path.lower()
    for scheme in _URL_SCHEMES:
        if lowered.startswith(scheme):
            return path[len(scheme) :]
    if lowered.startswith("git@github.com:"):
        return "github.com/" + path.split(":", 1)[1]
    if lowered.startswith("git@github.com/"):
        return "github.com/" + path[len("git@github.com/") :]
    for host in _URL_HOSTS:
        if lowered.startswith(host):
            return path[len(host) :]
    return path


def _canonical_github_clone_url(raw: str) -> str:
    """Normalise any common ``github_repo`` shape to ``https://github.com/owner/repo.git``.

    Accepts:
      - ``owner/repo`` or ``owner/repo.git``
      - ``https://github.com/owner/repo`` / ``...repo.git``
      - ``http://github.com/owner/repo`` (upgraded to HTTPS)
      - ``git@github.com:owner/repo.git`` (SSH form)
      - Any of the above wrapped in whitespace.

    Defends against the server-side double-prefix bug that produced
    ``https://github.com/https://github.com/owner/repo.git`` by stripping
    every ``github.com/`` (or SSH ``github.com:``) prefix, not just the
    first one, before validating ``owner/repo``.

    Raises ``ValueError`` when no ``owner/repo`` pair can be recovered.
    """
    stripped = raw.strip()
    if not stripped:
        msg = "empty string"
        raise ValueError(msg)

    # Iteratively peel the outermost prefix (scheme, user-info, or host) until
    # stable. One-pass stripping is not enough for the double-prefix bug where
    # a second URL is nested after the first `github.com/` hop.
    path = stripped
    while True:
        stepped = _strip_one_prefix(path)
        if stepped == path:
            break
        path = stepped
    # At this point `path` should be `owner/repo` (possibly with `.git`
    # or a trailing slash or URL junk we refuse to guess at).
    path = path.rstrip("/")
    if path.lower().endswith(".git"):
        path = path[: -len(".git")]
    match = _OWNER_REPO_RE.match(path)
    if match is None:
        msg = f"cannot extract owner/repo from {raw!r}"
        raise ValueError(msg)
    owner, repo = match.group(1), match.group(2)
    return f"https://github.com/{owner}/{repo}.git"


def _require_gh() -> None:
    """Translate a missing ``gh`` CLI into a ``WorkspaceError`` so callers see one exception type."""
    try:
        ensure_gh_installed()
    except RuntimeError as exc:
        raise WorkspaceError(str(exc)) from exc


def _git(args: list[str], *, cwd: Path | None = None, check: bool = True) -> subprocess.CompletedProcess[str]:
    """Run ``git`` and raise ``WorkspaceError`` on non-zero exit.

    Centralised so every git call gets the same text/capture treatment and
    the same error-to-exception mapping. Network-facing operations rely on
    ``gh``'s git credential helper, so callers must have already invoked
    ``_require_gh`` (``sync`` does this for the whole workspace at start).
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
