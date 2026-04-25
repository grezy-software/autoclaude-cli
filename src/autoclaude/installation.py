"""Stable per-machine identity for the daemon heartbeat.

We persist a UUID alongside the config so the same install reports the same
``installation_id`` across reboots; the server upserts a RunnerInstallation
row keyed on (user, installation_id).
"""

from __future__ import annotations

import json
import socket
import sys
import uuid
from dataclasses import dataclass
from pathlib import Path

from autoclaude.config import config_dir

INSTALLATION_FILENAME = "installation.json"


@dataclass(frozen=True)
class InstallationIdentity:
    installation_id: str
    hostname: str
    os_platform: str


def installation_path() -> Path:
    return config_dir() / INSTALLATION_FILENAME


def _detect_hostname() -> str:
    try:
        return socket.gethostname() or ""
    except OSError:
        return ""


def get_or_create_identity() -> InstallationIdentity:
    """Return the persisted identity, generating a fresh UUID on first call.

    Hostname and ``sys.platform`` are re-read on every call because they can
    change without invalidating the install (laptop renamed, dual-boot).
    """
    path = installation_path()
    if path.exists():
        try:
            data = json.loads(path.read_text())
            stored_id = str(data.get("installation_id") or "").strip()
        except (OSError, ValueError, json.JSONDecodeError):
            stored_id = ""
        if stored_id:
            return InstallationIdentity(
                installation_id=stored_id,
                hostname=_detect_hostname(),
                os_platform=sys.platform,
            )
    new_id = uuid.uuid4().hex
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"installation_id": new_id}, indent=2))
    return InstallationIdentity(
        installation_id=new_id,
        hostname=_detect_hostname(),
        os_platform=sys.platform,
    )


__all__ = ["INSTALLATION_FILENAME", "InstallationIdentity", "get_or_create_identity", "installation_path"]
