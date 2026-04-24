"""Apply a manifest to ``~/.claude`` idempotently.

The applier writes every file the manifest declares under the user's
``~/.claude`` directory. Writes are idempotent: identical content is a
no-op, letting us run reconciliation on every CLI startup cheaply.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from autoclaude.storage import atomic_write_text
from autoclaude.tools.manifest import Manifest

_CLAUDE_DIR_NAME = ".claude"
_COMMANDS_SUBDIR = "commands"
_DOCS_SUBDIR = "autoclaude-docs"
_SKILLS_SUBDIR = "skills"


@dataclass
class ToolApplier:
    """Write a manifest to ``~/.claude`` under per-tool subfolders.

    ``home`` defaults to the real user's home directory; tests override it.
    ``subpath`` lets the caller scope writes under a subfolder (e.g.,
    ``autoclaude``) so we don't collide with unrelated user config.
    """

    home: Path
    subpath: str = "autoclaude"

    @property
    def base(self) -> Path:
        return self.home / _CLAUDE_DIR_NAME

    @property
    def commands_dir(self) -> Path:
        return self.base / _COMMANDS_SUBDIR

    def docs_dir_for(self, slug: str) -> Path:
        return self.base / _DOCS_SUBDIR / self.subpath / slug

    @property
    def skills_dir(self) -> Path:
        return self.base / _SKILLS_SUBDIR

    def apply(self, manifest: Manifest) -> list[Path]:
        """Write every primitive in ``manifest``; return the list of touched paths."""
        touched: list[Path] = []
        for cmd in manifest.commands:
            name = str(cmd.get("name") or "").strip()
            body = str(cmd.get("body") or "")
            if not name:
                continue
            target = self.commands_dir / f"{name}.md"
            if _write_if_changed(target, body):
                touched.append(target)
        for doc in manifest.docs:
            path_raw = str(doc.get("path") or "").strip().lstrip("/")
            body = str(doc.get("body") or "")
            if not path_raw:
                continue
            target = self.docs_dir_for(manifest.slug) / path_raw
            if _write_if_changed(target, body):
                touched.append(target)
        for skill in manifest.skills:
            name = str(skill.get("name") or "").strip()
            body = str(skill.get("body") or "")
            if not name:
                continue
            target = self.skills_dir / name / "SKILL.md"
            if _write_if_changed(target, body):
                touched.append(target)
        # Plugins and mcpServers are advisory for v1: the CLI does not mutate
        # the user's MCP config or plugin list. They surface via ``manifest.prompt``
        # so a human (or claude) can install them.
        return touched


def _write_if_changed(target: Path, body: str) -> bool:
    """Write ``body`` to ``target`` only if the content differs. Returns ``True`` on write."""
    if target.exists():
        try:
            existing = target.read_text(encoding="utf-8")
        except OSError:
            existing = None
        if existing == body:
            return False
    atomic_write_text(target, body)
    return True


def apply_manifest(home: Path, manifest: Manifest, *, subpath: str = "autoclaude") -> list[Path]:
    """Convenience helper used by the runner and by tests."""
    return ToolApplier(home=home, subpath=subpath).apply(manifest)
