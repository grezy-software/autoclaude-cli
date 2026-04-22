"""Invoke the ``claude`` CLI for one agent step.

Uses ``claude -p <prompt> --output-format json`` so we can capture the
session id and, when available, the cost.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path


@dataclass
class ClaudeResult:
    ok: bool
    stdout: str
    stderr: str
    session_id: str = ""
    total_cost_usd: float = 0.0


def run_step(prompt: str, *, cwd: Path, timeout: int = 3600) -> ClaudeResult:
    proc = subprocess.run(
        ["claude", "-p", prompt, "--output-format", "json"],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        check=False,
        timeout=timeout,
    )
    session_id = ""
    cost = 0.0
    if proc.stdout:
        try:
            parsed = json.loads(proc.stdout)
            session_id = str(parsed.get("session_id") or parsed.get("sessionId") or "")
            cost = float(parsed.get("total_cost_usd") or parsed.get("totalCostUsd") or 0.0)
        except (ValueError, TypeError):
            pass
    return ClaudeResult(
        ok=proc.returncode == 0,
        stdout=proc.stdout,
        stderr=proc.stderr,
        session_id=session_id,
        total_cost_usd=cost,
    )
