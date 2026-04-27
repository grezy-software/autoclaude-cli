"""Capture Claude Code rate_limits via a managed status line script.

Claude Code renders a status line after every assistant turn and pipes a JSON
payload to it on stdin. That payload includes the caller's subscription
``rate_limits`` (5-hour session %, 7-day weekly %) -- but only in interactive
mode and not via ``claude -p`` (verified empirically).

To get this signal into AutoClaude without prying open the OAuth keychain,
this module:

1. Installs a tiny shell script that the user's ``~/.claude/settings.json``
   registers as the status line. The script extracts ``rate_limits`` and
   writes it (alongside the previous status line text, if any) to
   ``~/.autoclaude/claude_usage.json`` atomically.
2. Provides ``read_latest_usage()`` that returns the most recent sample so
   the daemon can ship it to the server every 15 minutes.

The installer is non-destructive: if the user already has a non-AutoClaude
status line configured, we leave it alone and surface a one-line warning so
they can wire ours in manually.
"""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path
from typing import Any

from autoclaude.logger import get_logger

_log = get_logger("usage")

_CLAUDE_DIR = Path.home() / ".claude"
_CLAUDE_SETTINGS = _CLAUDE_DIR / "settings.json"
_AUTOCLAUDE_DIR = Path.home() / ".autoclaude"
_USAGE_CACHE_PATH = _AUTOCLAUDE_DIR / "claude_usage.json"
_STATUSLINE_SCRIPT_PATH = _AUTOCLAUDE_DIR / "claude_statusline.sh"

# Marker we embed in the status line script so we can detect "this is our
# managed script, safe to overwrite/upgrade" vs. "this is a user script we
# should not touch".
_AUTOCLAUDE_MARKER = "# autoclaude-managed-statusline v1"

_STATUSLINE_SCRIPT = f"""#!/bin/bash
{_AUTOCLAUDE_MARKER}
# Reads Claude Code's status line JSON from stdin, captures rate_limits to
# ~/.autoclaude/claude_usage.json, and prints a short status string.
set -euo pipefail
input=$(cat)
mkdir -p "$HOME/.autoclaude"
tmp="$HOME/.autoclaude/claude_usage.json.tmp"
echo "$input" | python3 -c '
import json, sys, time
data = json.load(sys.stdin)
rl = (data or {{}}).get("rate_limits") or {{}}
five = rl.get("five_hour") or {{}}
seven = rl.get("seven_day") or {{}}
out = {{
    "captured_at": time.time(),
    "five_hour_pct": five.get("used_percentage"),
    "seven_day_pct": seven.get("used_percentage"),
    "five_hour_resets_at": five.get("resets_at"),
    "seven_day_resets_at": seven.get("resets_at"),
}}
print(json.dumps(out))
' > "$tmp" 2>/dev/null && mv "$tmp" "$HOME/.autoclaude/claude_usage.json" || true

# Print a compact status string so the user sees something useful.
echo "$input" | python3 -c '
import json, sys
try:
    d = json.load(sys.stdin)
    rl = (d or {{}}).get("rate_limits") or {{}}
    parts = []
    f = (rl.get("five_hour") or {{}}).get("used_percentage")
    s = (rl.get("seven_day") or {{}}).get("used_percentage")
    if f is not None: parts.append(f"5h:{{int(f)}}%")
    if s is not None: parts.append(f"7d:{{int(s)}}%")
    print(" ".join(parts))
except Exception:
    pass
' || true
"""


def install_statusline() -> str:
    """Ensure the managed status line script and ~/.claude/settings.json entry exist.

    Returns a short human-readable status: ``installed``, ``upgraded``,
    ``already_present``, ``conflict`` (user has their own status line; we
    don't touch it), or ``error``.
    """
    try:
        _AUTOCLAUDE_DIR.mkdir(parents=True, exist_ok=True)
        existing = _STATUSLINE_SCRIPT_PATH.read_text() if _STATUSLINE_SCRIPT_PATH.exists() else ""
        if existing != _STATUSLINE_SCRIPT:
            _STATUSLINE_SCRIPT_PATH.write_text(_STATUSLINE_SCRIPT)
            _STATUSLINE_SCRIPT_PATH.chmod(_STATUSLINE_SCRIPT_PATH.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP)
        return _wire_settings_json()
    except OSError as exc:
        _log.warning("status line install failed: %s", exc, extra={"source": "cli"})
        return "error"


def _wire_settings_json() -> str:
    """Add the statusLine entry to ~/.claude/settings.json without clobbering user config."""
    if not _CLAUDE_DIR.exists():
        # Claude Code isn't installed for this user; nothing to wire.
        return "error"
    settings: dict[str, Any] = {}
    if _CLAUDE_SETTINGS.exists():
        try:
            settings = json.loads(_CLAUDE_SETTINGS.read_text() or "{}")
        except (ValueError, OSError):
            settings = {}
    if not isinstance(settings, dict):
        settings = {}

    desired = {"type": "command", "command": str(_STATUSLINE_SCRIPT_PATH)}
    current = settings.get("statusLine")

    if current == desired:
        return "already_present"
    if isinstance(current, dict) and current.get("command") == str(_STATUSLINE_SCRIPT_PATH):
        # Same script, normalise shape.
        settings["statusLine"] = desired
        _atomic_write_json(_CLAUDE_SETTINGS, settings)
        return "upgraded"
    if isinstance(current, dict):
        # User has their own status line; don't overwrite -- log and bail.
        _log.warning(
            "user has a custom statusLine in ~/.claude/settings.json; skipping autoclaude install. "
            "wire %s manually if you want claude usage tracking.",
            _STATUSLINE_SCRIPT_PATH,
            extra={"source": "cli"},
        )
        return "conflict"
    settings["statusLine"] = desired
    _atomic_write_json(_CLAUDE_SETTINGS, settings)
    return "installed"


def _atomic_write_json(path: Path, payload: dict) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2))
    os.replace(tmp, path)


def read_latest_usage(*, max_age_seconds: float | None = None) -> dict[str, Any] | None:
    """Return the most recent rate_limits sample, or None when nothing usable.

    ``max_age_seconds`` lets the caller filter out stale data: if the file
    hasn't been touched within that window, we return None rather than
    shipping a percentage that no longer reflects the user's quota.
    """
    if not _USAGE_CACHE_PATH.exists():
        return None
    try:
        data = json.loads(_USAGE_CACHE_PATH.read_text() or "{}")
    except (ValueError, OSError):
        return None
    if not isinstance(data, dict):
        return None
    if max_age_seconds is not None:
        captured = data.get("captured_at")
        if not isinstance(captured, (int, float)):
            return None
        import time

        if time.time() - captured > max_age_seconds:
            return None
    if data.get("five_hour_pct") is None and data.get("seven_day_pct") is None:
        return None
    return {
        "five_hour_pct": data.get("five_hour_pct"),
        "seven_day_pct": data.get("seven_day_pct"),
        "five_hour_resets_at": data.get("five_hour_resets_at"),
        "seven_day_resets_at": data.get("seven_day_resets_at"),
    }


__all__ = [
    "install_statusline",
    "read_latest_usage",
]
