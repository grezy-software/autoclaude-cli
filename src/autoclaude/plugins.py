"""Ensure required Claude Code plugins are installed before each tick.

Relies on the ``claude`` CLI. Installation is idempotent: the CLI lists
already-installed plugins and only calls ``claude plugin install`` for the
missing ones.
"""

from __future__ import annotations

import subprocess  # noqa: S404


def _claude(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["claude", *args],
        capture_output=True,
        text=True,
        check=False,
    )


def list_installed() -> list[str]:
    proc = _claude("plugin", "list")
    if proc.returncode != 0:
        return []
    return [line.strip() for line in proc.stdout.splitlines() if line.strip()]


def ensure_installed(refs: list[str]) -> list[str]:
    """Install missing plugins. Returns the list of refs we asked to install."""
    if not refs:
        return []
    installed = set(list_installed())
    to_install = [ref for ref in refs if ref not in installed]
    for ref in to_install:
        proc = _claude("plugin", "install", ref)
        if proc.returncode != 0:
            msg = proc.stderr.strip() or proc.stdout.strip() or f"claude plugin install {ref} failed"
            raise RuntimeError(msg)
    return to_install
