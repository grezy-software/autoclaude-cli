"""Environment detection and privilege-drop helpers for the ``claude`` subprocess.

The ``claude`` CLI refuses to combine ``--permission-mode bypassPermissions`` (or
``--dangerously-skip-permissions``) with an effective UID of 0. When the host
runs autoclaude as root (common in WSL2 and minimal containers) we need to:

1. Skip the ``--permission-mode bypassPermissions`` flag if the user already has
   ``permissions.defaultMode == "auto"`` in their ``settings.json`` -- claude
   handles permission elision itself in that mode.
2. Drop privileges before exec'ing ``claude``: ensure a dedicated ``autoclaude``
   user exists, share the existing ``~/.claude`` config and the working repo
   with that user via a shared ``autoclaude`` group, then wrap the argv with
   ``runuser -u autoclaude --preserve-environment --``.

All helpers in this module are idempotent and cache per-process so repeated
``run_step`` calls do not re-run ``chgrp -R`` on a 50k-file checkout.
"""

from __future__ import annotations

import grp
import json
import os
import pwd
import shutil
import subprocess
from pathlib import Path
from typing import Final

from autoclaude.logger import get_logger

_log = get_logger("claude_env")

AUTOCLAUDE_USER: Final[str] = "autoclaude"
AUTOCLAUDE_GROUP: Final[str] = "autoclaude"
ISSUE_URL: Final[str] = "https://github.com/grezy-software/autoclaude-cli/issues"

# claude rotates this file (refresh tokens, etc.) and writes it back with
# mode 0600 owned by root:root, which silently breaks the autoclaude user's
# read access. We re-grant group read perms before every tick — see
# :func:`share_claude_credentials`.
CREDENTIALS_FILENAME: Final[str] = ".credentials.json"

# Per-process caches: avoid repeating expensive filesystem walks every tick.
_shared_repos: set[str] = set()
_shared_config: bool = False
_shared_binary: bool = False
_shared_gh_config: bool = False
_shared_workspace_home: bool = False
_traversal_granted: set[str] = set()
_logged_modes: set[str] = set()


class UserCreationError(RuntimeError):
    """Raised when the ``autoclaude`` user cannot be provisioned on this host."""


def _remediation(detail: str) -> str:
    return f"{detail} Please open an issue at {ISSUE_URL} with your OS / container details so we can add support."


def _read_settings_file(path: Path) -> dict:
    """Best-effort JSON read; returns ``{}`` on any failure."""
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return {}
    try:
        parsed = json.loads(text)
    except (ValueError, TypeError):
        return {}
    if not isinstance(parsed, dict):
        return {}
    return parsed


def read_default_permission_mode(*, home: Path | None = None, cwd: Path | None = None) -> str | None:
    """Resolve the effective ``permissions.defaultMode`` for ``claude``.

    Reads ``~/.claude/settings.json`` first, then ``<cwd>/.claude/settings.json``
    layered on top so a project-level setting wins over the user-level setting,
    matching how claude itself merges them. Returns ``None`` when neither file
    sets the key.
    """
    home = home if home is not None else Path.home()
    cwd = cwd if cwd is not None else Path.cwd()
    candidates = [
        home / ".claude" / "settings.json",
        cwd / ".claude" / "settings.json",
    ]
    mode: str | None = None
    for candidate in candidates:
        data = _read_settings_file(candidate)
        perms = data.get("permissions")
        if not isinstance(perms, dict):
            continue
        value = perms.get("defaultMode")
        if isinstance(value, str):
            mode = value
    return mode


def should_bypass_permissions(*, home: Path | None = None, cwd: Path | None = None) -> bool:
    """``True`` when we must pass ``--permission-mode bypassPermissions`` to claude.

    ``False`` only when the user has explicitly opted into ``defaultMode=auto``,
    in which case claude itself handles permission elision and the flag is
    redundant (and would duplicate auto-mode semantics).
    """
    return read_default_permission_mode(home=home, cwd=cwd) != "auto"


def is_root() -> bool:
    """``True`` when the current process effective UID is 0."""
    return os.geteuid() == 0


def autoclaude_user_exists() -> bool:
    """Public probe for whether the dedicated ``autoclaude`` system user exists.

    The runner uses this at tick time to decide whether to bail out with a
    "run ``autoclaude init --user-autoclaude``" message rather than provisioning
    the user mid-tick (which would surprise the operator and slow the first
    step).
    """
    return _user_exists(AUTOCLAUDE_USER)


def _user_exists(username: str) -> bool:
    try:
        pwd.getpwnam(username)
    except KeyError:
        return False
    return True


def _group_exists(groupname: str) -> bool:
    try:
        grp.getgrnam(groupname)
    except KeyError:
        return False
    return True


def _root_in_group(groupname: str) -> bool:
    try:
        group = grp.getgrnam(groupname)
    except KeyError:
        return False
    return "root" in group.gr_mem


def _run_cmd(argv: list[str], *, log_failure: bool = True) -> bool:
    """Run ``argv`` and return ``True`` iff it exited 0. Never raises."""
    try:
        result = subprocess.run(argv, check=False, capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.SubprocessError) as exc:
        if log_failure:
            _log.warning("command %s failed to start: %s", argv[0], exc)
        return False
    if result.returncode != 0:
        if log_failure:
            _log.warning(
                "command %s exited rc=%s stderr=%s",
                argv,
                result.returncode,
                result.stderr.strip(),
            )
        return False
    return True


def ensure_autoclaude_user(
    username: str = AUTOCLAUDE_USER,
    group: str = AUTOCLAUDE_GROUP,
) -> None:
    """Provision the ``autoclaude`` group + user; add ``root`` to the group.

    Idempotent: existing entities are left alone. Raises :class:`UserCreationError`
    with a clear remediation message if neither ``groupadd``/``useradd`` nor
    ``addgroup``/``adduser`` are available on the host.
    """
    if not _group_exists(group):
        _log.info("creating system group '%s'", group)
        if not _run_cmd(["groupadd", group], log_failure=False) and not _run_cmd(
            ["addgroup", group],
            log_failure=False,
        ):
            raise UserCreationError(
                _remediation(
                    f"Cannot create group '{group}': neither `groupadd` nor `addgroup` is available.",
                ),
            )
    if not _user_exists(username):
        _log.info("creating system user '%s' for sandboxed claude execution", username)
        created = _run_cmd(
            ["useradd", "-m", "-s", "/bin/bash", "-g", group, username],
            log_failure=False,
        )
        if not created:
            created = _run_cmd(
                [
                    "adduser",
                    "--system",
                    "--ingroup",
                    group,
                    "--shell",
                    "/bin/bash",
                    "--home",
                    f"/home/{username}",
                    username,
                ],
                log_failure=False,
            )
        if not created:
            raise UserCreationError(
                _remediation(
                    f"Cannot create user '{username}': neither `useradd` nor `adduser` is available.",
                ),
            )
    if not _root_in_group(group):
        _log.info("adding root to group '%s'", group)
        if not _run_cmd(["usermod", "-aG", group, "root"], log_failure=False) and not _run_cmd(
            ["adduser", "root", group],
            log_failure=False,
        ):
            _log.warning(
                "failed to add root to group '%s'; group writes from root may be denied",
                group,
            )


def _has_other_execute(path: Path) -> bool:
    """``True`` when world-execute (o+x) is set, meaning anyone can traverse."""
    try:
        mode = path.stat().st_mode
    except OSError:
        return False
    return bool(mode & 0o001)


def _grant_path_traversal(path: Path, *, group: str = AUTOCLAUDE_GROUP) -> None:
    """Grant ``group`` traversal (``g+x``) on every directory ancestor of ``path``.

    The autoclaude user can only ``execve`` ``/root/.local/bin/claude`` if it can
    *traverse* every component of that path. ``/root`` defaults to ``0700`` and
    blocks the kernel's path lookup before it ever reaches the binary, producing
    a generic ``Permission denied`` from ``runuser``.

    For each ancestor up to ``/``: chgrp to ``group`` and ``chmod g+x``. Any
    ancestor that is already world-executable (``/home``, ``/usr``, ``/tmp``) is
    skipped to avoid surprising chgrp side effects on system directories. The
    leaf at ``path`` itself is given ``g+rx`` so callers can read/exec it.

    Idempotent and cached per absolute leaf path.
    """
    target = path.absolute()
    key = str(target)
    if key in _traversal_granted:
        return

    ancestors: list[Path] = []
    cur = target.parent
    while cur != cur.parent:  # stop before "/"
        ancestors.append(cur)
        cur = cur.parent

    # Apply from "/" downward so a missing chgrp on a top-level dir surfaces first.
    for ancestor in reversed(ancestors):
        if _has_other_execute(ancestor):
            continue
        _run_cmd(["chgrp", group, str(ancestor)])
        _run_cmd(["chmod", "g+x", str(ancestor)])

    if target.exists() and not _has_other_execute(target):
        _run_cmd(["chgrp", group, str(target)])
        if target.is_dir():
            _run_cmd(["chmod", "g+rx", str(target)])
        else:
            _run_cmd(["chmod", "g+rx", str(target)])
    _traversal_granted.add(key)


def share_claude_config(
    *,
    username: str = AUTOCLAUDE_USER,
    group: str = AUTOCLAUDE_GROUP,
    home: Path | None = None,
) -> None:
    """Symlink ``~<username>/.claude`` -> root's ``.claude`` and grant group access.

    Single source of truth: the autoclaude user reads/writes the same config the
    operator already authenticated with. Cached per-process; subsequent calls
    are no-ops.
    """
    global _shared_config  # noqa: PLW0603
    if _shared_config:
        return
    home = home if home is not None else Path.home()
    src = home / ".claude"
    if not src.exists():
        _log.warning("%s does not exist; skipping claude config share", src)
        _shared_config = True
        return
    _run_cmd(["chgrp", "-R", group, str(src)])
    _run_cmd(["chmod", "-R", "g+rwX", str(src)])
    # Grant ancestor traversal so the autoclaude user can actually descend into
    # this directory: chgrp on the leaf is useless if /root above it is 0700.
    _grant_path_traversal(src, group=group)
    try:
        target_home = Path(pwd.getpwnam(username).pw_dir)
    except KeyError:
        _shared_config = True
        return
    target = target_home / ".claude"
    if not target.exists() and not target.is_symlink():
        try:
            target_home.mkdir(parents=True, exist_ok=True)
            target.symlink_to(src, target_is_directory=True)
            _run_cmd(["chown", "-h", f"{username}:{group}", str(target)])
            _log.info("symlinked %s -> %s", target, src)
        except OSError as exc:
            _log.warning("failed to symlink claude config to %s: %s", target, exc)
    _shared_config = True


def share_claude_credentials(*, group: str = AUTOCLAUDE_GROUP, home: Path | None = None) -> None:
    """Re-grant ``group`` read access to ``~/.claude/.credentials.json``.

    Claude rotates the credentials file (refresh tokens, OAuth flow, login)
    and writes it back as ``mode 0600`` owned by ``root:root``, silently
    erasing the group permissions ``share_claude_config`` set up at startup.
    The next tick that runs as the ``autoclaude`` user then exits with
    ``Not logged in`` -- and worse, in ``--output-format stream-json``
    mode it produces no output at all.

    Unlike ``share_claude_config``, this helper is **not** cached: it must
    re-apply on every tick because claude can rewrite the file at any time.
    The cost is a single ``chgrp`` + ``chmod`` on one small file, well below
    the noise floor of a tick.
    """
    home = home if home is not None else Path.home()
    creds = home / ".claude" / CREDENTIALS_FILENAME
    if not creds.exists():
        return
    _run_cmd(["chgrp", group, str(creds)])
    _run_cmd(["chmod", "g+r", str(creds)])


def share_gh_config(
    *,
    username: str = AUTOCLAUDE_USER,
    group: str = AUTOCLAUDE_GROUP,
    home: Path | None = None,
) -> None:
    """Symlink ``~<username>/.config/gh`` -> root's ``.config/gh`` and grant group access.

    The autoclaude-wrapped claude inherits ``HOME=/home/autoclaude``, so any
    ``gh`` invocation inside an agent step looks up its config under
    ``/home/autoclaude/.config/gh/`` and reports "not logged in" even though
    the operator authenticated ``gh`` as root before launching autoclaude.
    Mirrors :func:`share_claude_config`: chgrp the source dir to the shared
    group, grant ancestor traversal, then expose it to the autoclaude user via
    a symlink. Cached per process.
    """
    global _shared_gh_config  # noqa: PLW0603
    if _shared_gh_config:
        return
    home = home if home is not None else Path.home()
    src = home / ".config" / "gh"
    if not src.exists():
        _shared_gh_config = True
        return
    _run_cmd(["chgrp", "-R", group, str(src)])
    _run_cmd(["chmod", "-R", "g+rwX", str(src)])
    _grant_path_traversal(src, group=group)
    try:
        target_home = Path(pwd.getpwnam(username).pw_dir)
    except KeyError:
        _shared_gh_config = True
        return
    target_config = target_home / ".config"
    target = target_config / "gh"
    if not target.exists() and not target.is_symlink():
        try:
            target_config.mkdir(parents=True, exist_ok=True)
            _run_cmd(["chown", f"{username}:{group}", str(target_config)])
            target.symlink_to(src, target_is_directory=True)
            _run_cmd(["chown", "-h", f"{username}:{group}", str(target)])
            _log.info("symlinked %s -> %s", target, src)
        except OSError as exc:
            _log.warning("failed to symlink gh config to %s: %s", target, exc)
    _shared_gh_config = True


def share_workspace_home(
    *,
    group: str = AUTOCLAUDE_GROUP,
    home: Path | None = None,
) -> None:
    """Grant the ``autoclaude`` group rwX on ``~/.autoclaude`` with setgid inheritance.

    The autoclaude workspace (``~/.autoclaude``, see ``workspace.workspace_home``)
    holds per-project clones (``repos/<slug>/``) and per-tick worktrees
    (``worktrees/<slug>/<tick_id>/``) created by the runner running as ``root``.
    The autoclaude user must be able to traverse and operate on these paths once
    ``runuser`` drops privileges.

    Applies recursively:
      - ``chgrp -R <group>`` on the whole tree
      - ``chmod -R g+rwX`` so the group can read, write, and traverse
      - ``chmod g+s`` on every directory (POSIX setgid) so newly created entries
        inherit ``group=<group>`` automatically. setgid is only set on directories
        because on regular files it has different semantics (run-as-group).

    Creates the directory if missing so the setgid bit applies *before* any clone
    or worktree lands there. Cached per-process; install-time only.
    """
    global _shared_workspace_home  # noqa: PLW0603
    if _shared_workspace_home:
        return
    home = home if home is not None else Path.home()
    workspace = home / ".autoclaude"
    workspace.mkdir(parents=True, exist_ok=True)
    _run_cmd(["chgrp", "-R", group, str(workspace)])
    _run_cmd(["chmod", "-R", "g+rwX", str(workspace)])
    _run_cmd(["find", str(workspace), "-type", "d", "-exec", "chmod", "g+s", "{}", "+"])
    _grant_path_traversal(workspace, group=group)
    _shared_workspace_home = True


def share_repo(cwd: Path, *, group: str = AUTOCLAUDE_GROUP) -> None:
    """Grant the ``autoclaude`` group rwX on the repo working directory.

    Cached per-process per-cwd: a 50k-file checkout would otherwise pay the
    ``chgrp -R`` cost on every tick.
    """
    key = str(cwd.resolve())
    if key in _shared_repos:
        return
    _run_cmd(["chgrp", "-R", group, str(cwd)])
    _run_cmd(["chmod", "-R", "g+rwX", str(cwd)])
    _shared_repos.add(key)


def share_claude_binary(*, group: str = AUTOCLAUDE_GROUP) -> None:
    """Grant ``group`` access to the ``claude`` binary and every directory above it.

    The binary often lives under ``/root/.local/bin/claude`` (a symlink into
    ``/root/.local/share/claude/versions/<v>``). Both the symlink path AND the
    resolved target path need traversable ancestors -- the kernel walks each
    component on ``execve``. Cached per process so repeated ticks do not re-chgrp
    the install tree.
    """
    global _shared_binary  # noqa: PLW0603
    if _shared_binary:
        return
    binary = shutil.which("claude")
    if not binary:
        _log.warning("claude binary not found on PATH; skipping binary share")
        _shared_binary = True
        return
    link_path = Path(binary)
    _grant_path_traversal(link_path, group=group)
    try:
        target_path = link_path.resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        _log.warning("could not resolve claude symlink at %s: %s", link_path, exc)
        target_path = link_path
    if target_path != link_path:
        _grant_path_traversal(target_path, group=group)
    _shared_binary = True


def autoclaude_subprocess_env_overrides(*, username: str = AUTOCLAUDE_USER) -> dict[str, str]:
    """Env vars that must override the parent's when wrapping with ``runuser``.

    ``runuser --preserve-environment`` keeps the *parent*'s env, including
    ``HOME=/root``. Claude resolves session/IPC/lock paths from ``$HOME/.claude``,
    so under that env the autoclaude-wrapped claude reads and locks the same
    files the parent claude (running as root) is already holding open --
    deadlocking in ``do_epoll_wait`` with no output. Forcing ``HOME`` to the
    autoclaude user's actual home directory resolves the symlink
    ``/home/autoclaude/.claude -> /root/.claude`` for credentials/config (single
    source of truth) while giving the new claude its own session/memory paths
    (``/home/autoclaude/.claude/projects/...``) that don't collide with the
    parent.

    Falls back to ``/home/<username>`` if the user isn't in ``pwd``; the runner
    bails out earlier in that case (``autoclaude_user_exists`` guard), so this
    is just defensive.
    """
    try:
        home = pwd.getpwnam(username).pw_dir
    except KeyError:
        home = f"/home/{username}"
    return {"HOME": home}


def _resolve_home(username: str) -> str:
    """Best-effort resolution of ``username``'s home dir. Falls back to ``/home/<user>``."""
    try:
        return pwd.getpwnam(username).pw_dir
    except KeyError:
        return f"/home/{username}"


def wrap_for_user(argv: list[str], *, username: str = AUTOCLAUDE_USER) -> list[str]:
    """Prefix ``argv`` so it runs as ``username`` while keeping the parent env.

    Prefers ``runuser`` (no PAM session, fewer surprises); falls back to
    ``sudo -E -u`` when ``runuser`` is missing. Raises :class:`UserCreationError`
    when neither tool is available.

    ``HOME`` is forced to the dropped user's home via an explicit ``env``
    prefix so the value is visible in the launch command (logs, ps output,
    error reporting) and does not silently depend on the parent's
    ``subprocess.Popen(env=...)`` propagation. ``--preserve-environment``
    keeps the rest of the parent's env (PATH, API tokens, etc.); the trailing
    ``env HOME=...`` overrides only the one var that must change.
    """
    home = _resolve_home(username)
    env_prefix = ["env", f"HOME={home}"]
    if shutil.which("runuser"):
        return ["runuser", "-u", username, "--preserve-environment", "--", *env_prefix, *argv]
    if shutil.which("sudo"):
        _log.warning("`runuser` not found; falling back to `sudo -E -u %s`", username)
        return ["sudo", "-E", "-u", username, "--", *env_prefix, *argv]
    raise UserCreationError(
        _remediation(
            f"Cannot drop privileges to '{username}': neither `runuser` nor `sudo` is available.",
        ),
    )


def share_per_tick_for_autoclaude_user(*, cwd: Path | None = None) -> None:
    """Apply every per-tick share so the autoclaude user inherits the operator's session.

    Single entry point that the runner, scheduler, and any future launch path
    can call so they cannot accidentally bypass one of the per-tick helpers.
    Idempotent and cached per-process; safe to call on every tick or service
    cycle. ``cwd`` is optional: when provided, the worktree is also shared so
    the autoclaude user can read/write the per-tick checkout. Install-time
    helpers (``share_claude_config``, ``share_claude_binary``) are deliberately
    excluded: ``autoclaude init`` owns those, and re-running ``chgrp -R`` on
    ``~/.claude`` mid-tick would be needlessly expensive.
    """
    share_claude_credentials()
    share_gh_config()
    if cwd is not None:
        share_repo(cwd)


def log_mode_once(message: str) -> None:
    """INFO-log ``message`` exactly once per process (deduped by content)."""
    if message in _logged_modes:
        return
    _logged_modes.add(message)
    _log.info("%s", message)


def summarize_runtime(*, home: Path | None = None, cwd: Path | None = None) -> dict[str, object]:
    """Snapshot of the runtime decisions ``run_step`` will make for ``diag``.

    Reports the resolved ``defaultMode`` from each settings file, whether
    ``--permission-mode bypassPermissions`` will be passed to claude, and which
    OS user will own the spawned ``claude`` subprocess.

    ``claude_runs_as`` reflects the actual current state of the system, not
    intent: ``autoclaude`` is only returned when (a) the runner would wrap with
    that user AND (b) the user actually exists on the host. When the wrapper is
    intended but the user has not been provisioned yet, ``claude_runs_as``
    reports the current effective uid's name and ``autoclaude_user_required`` /
    ``autoclaude_user_exists`` together signal that the next tick will provision
    it (or fail loudly if useradd is missing).
    """
    home = home if home is not None else Path.home()
    cwd = cwd if cwd is not None else Path.cwd()

    user_settings = home / ".claude" / "settings.json"
    project_settings = cwd / ".claude" / "settings.json"

    def _mode_from(path: Path) -> str:
        data = _read_settings_file(path)
        perms = data.get("permissions")
        if isinstance(perms, dict):
            value = perms.get("defaultMode")
            if isinstance(value, str):
                return value
        return "<unset>"

    user_mode = _mode_from(user_settings)
    project_mode = _mode_from(project_settings)
    effective_mode = read_default_permission_mode(home=home, cwd=cwd) or "<unset>"
    bypass = should_bypass_permissions(home=home, cwd=cwd)
    permission_mode = "bypassPermissions" if bypass else "<unset>"

    autoclaude_required = bypass and is_root()
    autoclaude_exists = _user_exists(AUTOCLAUDE_USER)

    try:
        current_user = pwd.getpwuid(os.geteuid()).pw_name
    except KeyError:
        current_user = f"uid={os.geteuid()}"

    run_as = AUTOCLAUDE_USER if autoclaude_required and autoclaude_exists else current_user

    return {
        "user_settings_path": str(user_settings),
        "user_settings_default_mode": user_mode,
        "project_settings_path": str(project_settings),
        "project_settings_default_mode": project_mode,
        "effective_default_mode": effective_mode,
        "claude_permission_mode": permission_mode,
        "claude_runs_as": run_as,
        "autoclaude_user_required": autoclaude_required,
        "autoclaude_user_exists": autoclaude_exists,
    }


def reset_caches() -> None:
    """Clear per-process caches. Test-only; not part of the runtime contract."""
    _shared_repos.clear()
    _logged_modes.clear()
    _traversal_granted.clear()
    global _shared_config, _shared_binary, _shared_gh_config, _shared_workspace_home  # noqa: PLW0603
    _shared_config = False
    _shared_binary = False
    _shared_gh_config = False
    _shared_workspace_home = False
