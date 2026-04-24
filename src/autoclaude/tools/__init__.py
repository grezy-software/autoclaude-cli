"""CLI-side reconciliation of server-driven tool manifests.

Each tool exposed by the AutoClaude server comes with a manifest describing
what local primitives (slash commands, docs, skills, plugins, MCP servers)
the user's ``~/.claude`` instance needs before agents can use it. The CLI
fetches those manifests and applies them idempotently at the start of every
tick.
"""

from __future__ import annotations

from autoclaude.tools.applier import ToolApplier, apply_manifest
from autoclaude.tools.manifest import Manifest, ManifestRef

__all__ = [
    "Manifest",
    "ManifestRef",
    "ToolApplier",
    "apply_manifest",
]
