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

# Per-process caches: avoid repeating expensive filesystem walks every tick.
_shared_repos: set[str] = set()
_shared_config: bool = False
_logged_modes: set[str] = set()


class UserCreationError(RuntimeError):
    """Raised when the ``autoclaude`` user cannot be provisioned on this host."""


def _remediation(detail: str) -> str:
    return (
        f"{detail} Please open an issue at {ISSUE_URL} with your OS / container "
        "details so we can add support."
    )


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


def wrap_for_user(argv: list[str], *, username: str = AUTOCLAUDE_USER) -> list[str]:
    """Prefix ``argv`` so it runs as ``username`` while keeping the parent env.

    Prefers ``runuser`` (no PAM session, fewer surprises); falls back to
    ``sudo -E -u`` when ``runuser`` is missing. Raises :class:`UserCreationError`
    when neither tool is available.
    """
    if shutil.which("runuser"):
        return ["runuser", "-u", username, "--preserve-environment", "--", *argv]
    if shutil.which("sudo"):
        _log.warning("`runuser` not found; falling back to `sudo -E -u %s`", username)
        return ["sudo", "-E", "-u", username, "--", *argv]
    raise UserCreationError(
        _remediation(
            f"Cannot drop privileges to '{username}': neither `runuser` nor `sudo` is available.",
        ),
    )


def log_mode_once(message: str) -> None:
    """INFO-log ``message`` exactly once per process (deduped by content)."""
    if message in _logged_modes:
        return
    _logged_modes.add(message)
    _log.info("%s", message)


def reset_caches() -> None:
    """Clear per-process caches. Test-only; not part of the runtime contract."""
    _shared_repos.clear()
    _logged_modes.clear()
    global _shared_config  # noqa: PLW0603
    _shared_config = False
