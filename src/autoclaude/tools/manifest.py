"""Dataclasses for tool install manifests and per-step references."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class ManifestRef:
    """Reference embedded in each plan step: tool slug + expected manifest hash."""

    slug: str
    manifest_hash: str

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> ManifestRef:
        return cls(slug=str(raw.get("slug", "")), manifest_hash=str(raw.get("manifest_hash", "")))


@dataclass
class Manifest:
    """Parsed manifest body returned by ``/api/ac/tools/manifest/``.

    The server is the source of truth for hashes. We only trust our local
    hash when reconciling; we don't recompute one client-side.
    """

    slug: str
    manifest_hash: str
    version: str = "1"
    docs: list[dict[str, Any]] = field(default_factory=list)
    commands: list[dict[str, Any]] = field(default_factory=list)
    skills: list[dict[str, Any]] = field(default_factory=list)
    plugins: list[dict[str, Any]] = field(default_factory=list)
    mcp_servers: list[dict[str, Any]] = field(default_factory=list)
    prompt: str = ""

    @classmethod
    def from_payload(cls, slug: str, manifest_hash: str, body: dict[str, Any]) -> Manifest:
        return cls(
            slug=slug,
            manifest_hash=manifest_hash,
            version=str(body.get("version", "1")),
            docs=list(body.get("docs") or []),
            commands=list(body.get("commands") or []),
            skills=list(body.get("skills") or []),
            plugins=list(body.get("plugins") or []),
            mcp_servers=list(body.get("mcpServers") or []),
            prompt=str(body.get("prompt") or ""),
        )
