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
from autoclaude.gh import gh as _run_gh
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

    def __init__(
        self,
        home: Path,
        slug: str,
        clone_url: str,
        *,
        owner_repo: str = "",
    ) -> None:
        self._home = home
        self._slug = slug
        self._clone_url = clone_url
        # Empty for local-path tests; populated when the workspace targets
        # github.com so ``sync`` can route the clone through ``gh``.
        self._owner_repo = owner_repo

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
        owner_repo = clone_url.removeprefix("https://github.com/").removesuffix(".git")
        return cls(
            home=home or workspace_home(),
            slug=derive_slug(github_repo),
            clone_url=clone_url,
            owner_repo=owner_repo,
        )

    @classmethod
    def for_local_path(cls, source: Path, *, home: Path | None = None) -> Workspace:
        """Test-only hook: clone from a local path instead of GitHub.

        Lets the test suite exercise the full workspace lifecycle offline.
        Production code paths must use ``for_github_repo``. Leaves
        ``owner_repo`` empty so ``sync`` falls back to plain ``git clone``
        rather than asking ``gh`` to resolve a non-GitHub URL.
        """
        resolved = source.resolve()
        slug_short = _SLUG_SAFE_RE.sub("-", resolved.name.lower()).strip("-") or "repo"
        digest = hashlib.sha1(str(resolved).encode("utf-8"), usedforsecurity=False).hexdigest()[:8]
        return cls(
            home=home or workspace_home(),
            slug=f"{slug_short[:_SLUG_MAX_LENGTH]}-{digest}",
            clone_url=str(resolved),
            owner_repo="",
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

        First call clones into ``clone_path``; subsequent calls re-pin
        ``origin`` (in case the project moved or the test fixture rebuilt
        the source) and fetch every ref. Returns the clone path.

        Authentication: GitHub clones go through ``gh repo clone``, which
        reuses the gh-CLI session token without prompting. Subsequent
        ``git fetch``es inject ``gh``'s credential helper for one
        invocation only via ``-c credential.helper='!gh auth git-credential'``,
        so we never touch the user's global gitconfig and never let plain
        ``git`` prompt for an HTTPS password. Local-path workspaces (test
        fixtures) skip both and use plain ``git clone``/``fetch``.
        """
        _require_gh()
        self.clone_path.parent.mkdir(parents=True, exist_ok=True)
        if not self.clone_path.exists():
            if self._owner_repo:
                _gh_clone(self._owner_repo, self.clone_path)
                _log.info(
                    "cloned %s -> %s (via gh)",
                    self._owner_repo,
                    self.clone_path,
                    extra={"source": "workspace"},
                )
            else:
                _git(["clone", self._clone_url, str(self.clone_path)])
                _log.info("cloned %s -> %s", self._clone_url, self.clone_path, extra={"source": "workspace"})
            return self.clone_path
        _git(["remote", "set-url", "origin", self._clone_url], cwd=self.clone_path)
        if self._owner_repo:
            _git([*_gh_credential_helper_args(), "fetch", "--prune", "origin"], cwd=self.clone_path)
        else:
            _git(["fetch", "--prune", "origin"], cwd=self.clone_path)
        return self.clone_path

    def ensure_remote_branch(self, branch: str) -> None:
        """Ensure ``origin/<branch>`` exists; seed it with an empty commit if not.

        Empty GitHub repos (just created, or pre-existing without commits)
        have no default-branch ref, so ``git worktree add ... origin/<branch>``
        fails with ``invalid reference: origin/<branch>``. This pushes a
        single empty seed commit on ``<branch>`` so subsequent ticks can
        fork from ``origin/<branch>`` normally. No-op for local-path
        workspaces (test fixtures): they own their refs already.
        """
        if not self._owner_repo:
            return
        probe = _git(
            ["rev-parse", "--verify", "--quiet", f"refs/remotes/origin/{branch}"],
            cwd=self.clone_path,
            check=False,
        )
        if probe.returncode == 0:
            return
        # Ref missing locally — try a targeted fetch first; the branch may exist
        # on the remote and just not have been pulled yet (e.g. a clone that
        # only fetched the default branch, or a stale clone).
        _git(
            [
                *_gh_credential_helper_args(),
                "fetch",
                "origin",
                f"+refs/heads/{branch}:refs/remotes/origin/{branch}",
            ],
            cwd=self.clone_path,
            check=False,
        )
        probe = _git(
            ["rev-parse", "--verify", "--quiet", f"refs/remotes/origin/{branch}"],
            cwd=self.clone_path,
            check=False,
        )
        if probe.returncode == 0:
            return
        _log.info(
            "remote has no %s; seeding empty commit and pushing",
            branch,
            extra={"source": "workspace"},
        )
        # Point HEAD at <branch> regardless of whatever empty-clone state we
        # landed in, then create a single empty commit. ``--allow-empty``
        # avoids needing a working tree change. Inline ``user.name``/
        # ``user.email`` so we never depend on (or mutate) global git config.
        _git(["symbolic-ref", "HEAD", f"refs/heads/{branch}"], cwd=self.clone_path)
        _git(
            [
                "-c",
                "user.email=autoclaude@local",
                "-c",
                "user.name=autoclaude",
                "commit",
                "--allow-empty",
                "-m",
                "Initial commit",
            ],
            cwd=self.clone_path,
        )
        _git(
            [*_gh_credential_helper_args(), "push", "-u", "origin", branch],
            cwd=self.clone_path,
        )
        _git(
            [*_gh_credential_helper_args(), "fetch", "--prune", "origin"],
            cwd=self.clone_path,
        )

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
        # A previous tick run may have left the branch behind (remove_worktree
        # preserves branches by design). On retry, drop it so `-b` succeeds.
        _git(["branch", "-D", branch], cwd=self.clone_path, check=False)
        # If forking from origin/<branch>, make sure the remote-tracking ref
        # actually resolves locally; on a freshly-created empty repo it may
        # not exist yet, so seed it with an empty commit. No-op when present.
        if base.startswith("origin/"):
            self.ensure_remote_branch(base.removeprefix("origin/"))
        _git(
            ["worktree", "add", "-b", branch, str(target), base],
            cwd=self.clone_path,
        )
        _register_safe_directory(target)
        _log.info("worktree %s on %s", target, branch, extra={"source": "workspace"})
        return Worktree(path=target, branch=branch)

    def push_branch(self, branch: str, *, force: bool = False) -> str:
        """Push ``branch`` to origin and return its remote tree URL.

        Always tracks the remote branch (``-u``) so a subsequent push from
        a resumed tick fast-forwards instead of recreating the upstream.
        Empty branches (no commits since fork) push cleanly as a thin ref
        update on GitHub; useful so the user can see the branch landing
        page even when the agent only posted comments.

        Raises ``WorkspaceError`` for non-GitHub workspaces (test fixtures
        cloned from a local path) since pushing back to a tmpdir would
        rewrite the test source. Production callers always have
        ``_owner_repo`` set.
        """
        if not self._owner_repo:
            msg = "push_branch is only supported for github_repo workspaces"
            raise WorkspaceError(msg)
        push_args = [*_gh_credential_helper_args(), "push"]
        if force:
            push_args.append("--force-with-lease")
        push_args += ["-u", "origin", branch]
        _git(push_args, cwd=self.clone_path)
        url = f"https://github.com/{self._owner_repo}/tree/{branch}"
        _log.info("pushed %s -> %s", branch, url, extra={"source": "workspace"})
        return url

    def commits_ahead(self, worktree_path: Path, base_ref: str) -> int:
        """Return number of commits on ``HEAD`` not in ``base_ref``.

        Used to decide whether a branch push is meaningful: if HEAD has
        no commits beyond the base, there is nothing to publish.
        """
        result = _git(
            ["rev-list", "--count", f"{base_ref}..HEAD"],
            cwd=worktree_path,
        )
        return int(result.stdout.strip() or "0")

    def branch_url(self, branch: str) -> str:
        """Return the GitHub web URL for ``branch`` on this workspace's repo.

        Empty for local-path workspaces (no GitHub origin).
        """
        if not self._owner_repo:
            return ""
        return f"https://github.com/{self._owner_repo}/tree/{branch}"

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


def _gh_clone(owner_repo: str, dest: Path) -> None:
    """Clone via ``gh repo clone`` so the gh-CLI session token is reused.

    Plain ``git clone https://github.com/...`` would prompt for a username
    and password unless the user has already run ``gh auth setup-git``.
    Routing through ``gh repo clone`` reads gh's stored credential
    directly and never prompts. We translate gh failures into
    ``WorkspaceError`` so ``run_tick`` reports a single exception type.
    """
    try:
        _run_gh(["repo", "clone", owner_repo, str(dest)])
    except RuntimeError as exc:
        raise WorkspaceError(f"gh repo clone {owner_repo} failed: {exc}") from exc


def _gh_credential_helper_args() -> list[str]:
    """``-c`` flags that hand git ``gh``'s credential helper for one call.

    Used for ``git fetch`` against a github.com origin so the operation
    reuses the gh session without us writing anything to the user's
    global gitconfig (which is what ``gh auth setup-git`` would do).
    The empty first override clears any inherited helper so prompts
    from a misconfigured one cannot fire before ours runs.
    """
    return [
        "-c",
        "credential.helper=",
        "-c",
        "credential.helper=!gh auth git-credential",
    ]


def _register_safe_directory(path: Path) -> None:
    """Mark ``path`` as a safe directory in git's global config.

    Without this, git refuses to operate on a checkout whose ``.git``
    ownership does not match the calling user (the "dubious ownership"
    check, ``CVE-2022-24765``). Autoclaude worktrees are created by
    ``root`` but accessed by the dedicated ``autoclaude`` user via the
    shared ``autoclaude`` group; that ownership mismatch fires the check
    on every git invocation made by claude.

    Registers the path in the running user's gitconfig and, when
    ``runuser`` is available, in the ``autoclaude`` user's gitconfig too
    so the unprivileged claude can run git inside the worktree without
    being blocked. Failures are swallowed: a missing ``git``, ``runuser``,
    or autoclaude user only means the safety net cannot be installed,
    never that worktree creation should fail.
    """
    abs_path = str(path.absolute())
    cmd = ["git", "config", "--global", "--add", "safe.directory", abs_path]
    subprocess.run(cmd, check=False, capture_output=True)
    if shutil.which("runuser"):
        subprocess.run(
            ["runuser", "-u", "autoclaude", "--", *cmd],
            check=False,
            capture_output=True,
        )


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
