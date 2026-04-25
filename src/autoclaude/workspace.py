"""Dedicated autoclaude workspace: clones + per-tick git worktrees.

The CLI never operates on the user's working checkout. Every project's
source-of-truth is its GitHub repo, identified by ``github_repo`` on the
server-side ``Project``. We clone that into
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


def derive_slug(github_repo: str) -> str:
    """Build a filesystem-safe slug from a ``owner/repo`` GitHub identifier.

    Combines a slugified ``owner-repo`` with a short hash of the canonical
    clone URL. The hash guards against accidental reuse of a stale clone
    when a repo is renamed/moved on GitHub: a different canonical URL
    yields a different slug, so we clone fresh into a new directory rather
    than fetching foreign refs into an old one.
    """
    canonical = _canonical_github_clone_url(github_repo)
    # Strip the suffix to make `<owner>-<repo>` legible in `~/.autoclaude/repos/`.
    short = canonical.removeprefix("https://github.com/").removesuffix(".git")
    base = _SLUG_SAFE_RE.sub("-", short.lower().replace("/", "-")).strip("-") or "repo"
    digest = hashlib.sha1(canonical.encode("utf-8"), usedforsecurity=False).hexdigest()[:8]
    return f"{base[:_SLUG_MAX_LENGTH]}-{digest}"


@dataclass(frozen=True)
class Worktree:
    path: Path
    branch: str


class WorkspaceError(RuntimeError):
    """Raised when clone/fetch/worktree operations fail."""


class Workspace:
    """Manage the per-project GitHub clone and its per-tick worktrees."""

    def __init__(self, home: Path, slug: str, clone_url: str) -> None:
        self._home = home
        self._slug = slug
        self._clone_url = clone_url

    @classmethod
    def for_github_repo(cls, github_repo: str, *, home: Path | None = None) -> Workspace:
        """Build a Workspace targeting ``github.com/<owner>/<repo>.git``.

        Raises ``WorkspaceError`` when ``github_repo`` cannot be normalised
        to a valid clone URL (e.g. the project has no ``github_repo`` set).
        """
        try:
            clone_url = _canonical_github_clone_url(github_repo)
        except ValueError as exc:
            raise WorkspaceError(f"invalid github_repo {github_repo!r}: {exc}") from exc
        return cls(
            home=home or workspace_home(),
            slug=derive_slug(github_repo),
            clone_url=clone_url,
        )

    @classmethod
    def for_local_path(cls, source: Path, *, home: Path | None = None) -> Workspace:
        """Test-only hook: clone from a local path instead of GitHub.

        Lets the test suite exercise the full workspace lifecycle offline.
        Production code paths must use ``for_github_repo``.
        """
        resolved = source.resolve()
        slug_short = _SLUG_SAFE_RE.sub("-", resolved.name.lower()).strip("-") or "repo"
        digest = hashlib.sha1(str(resolved).encode("utf-8"), usedforsecurity=False).hexdigest()[:8]
        return cls(
            home=home or workspace_home(),
            slug=f"{slug_short[:_SLUG_MAX_LENGTH]}-{digest}",
            clone_url=str(resolved),
        )

    @property
    def home(self) -> Path:
        return self._home

    @property
    def slug(self) -> str:
        return self._slug

    @property
    def clone_url(self) -> str:
        return self._clone_url

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

    def sync(self) -> Path:
        """Ensure the clone exists and is up to date with the canonical URL.

        First call clones the GitHub URL into ``clone_path``; subsequent
        calls re-pin ``origin`` (in case the project moved or the test
        fixture rebuilt the source) and fetch every ref. Returns the clone
        path. ``origin`` is GitHub itself, so ``gh`` resolves the right
        repo without an auxiliary remote.
        """
        _require_gh()
        self.clone_path.parent.mkdir(parents=True, exist_ok=True)
        if not self.clone_path.exists():
            _git(["clone", self._clone_url, str(self.clone_path)])
            _log.info("cloned %s -> %s", self._clone_url, self.clone_path, extra={"source": "workspace"})
            return self.clone_path
        _git(["remote", "set-url", "origin", self._clone_url], cwd=self.clone_path)
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
