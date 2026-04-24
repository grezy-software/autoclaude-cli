"""Tool applier: idempotent writes under ~/.claude; hash cache integration."""

from __future__ import annotations

from pathlib import Path

from autoclaude.storage import RepoStorage
from autoclaude.tools.applier import ToolApplier, apply_manifest
from autoclaude.tools.manifest import Manifest, ManifestRef


def _discord_manifest(hash_override: str = "h1") -> Manifest:
    return Manifest(
        slug="discord",
        manifest_hash=hash_override,
        docs=[{"path": "tools/discord.md", "body": "doc body"}],
        commands=[{"name": "discord-post", "body": "# /discord-post\nbody"}],
    )


def test_apply_writes_commands_and_docs(tmp_path: Path) -> None:
    applier = ToolApplier(home=tmp_path)
    touched = applier.apply(_discord_manifest())

    cmd_path = tmp_path / ".claude" / "commands" / "discord-post.md"
    doc_path = tmp_path / ".claude" / "autoclaude-docs" / "autoclaude" / "discord" / "tools" / "discord.md"
    assert cmd_path.exists()
    assert doc_path.exists()
    assert cmd_path.read_text() == "# /discord-post\nbody"
    assert doc_path.read_text() == "doc body"
    assert set(touched) == {cmd_path, doc_path}


def test_apply_is_idempotent(tmp_path: Path) -> None:
    applier = ToolApplier(home=tmp_path)
    applier.apply(_discord_manifest())
    touched_second = applier.apply(_discord_manifest())
    assert touched_second == []


def test_apply_rewrites_on_body_change(tmp_path: Path) -> None:
    applier = ToolApplier(home=tmp_path)
    applier.apply(_discord_manifest())
    manifest = _discord_manifest()
    manifest.commands = [{"name": "discord-post", "body": "updated"}]
    applier.apply(manifest)
    cmd_path = tmp_path / ".claude" / "commands" / "discord-post.md"
    assert cmd_path.read_text() == "updated"


def test_manifest_ref_from_dict_tolerates_missing_keys() -> None:
    ref = ManifestRef.from_dict({})
    assert ref.slug == ""
    assert ref.manifest_hash == ""


def test_storage_round_trips_tool_hashes(tmp_path: Path) -> None:
    storage = RepoStorage.from_repo(tmp_path)
    storage.ensure()
    assert storage.read_tool_hashes() == {}
    storage.write_tool_hashes({"discord": "h1"})
    assert storage.read_tool_hashes() == {"discord": "h1"}


def test_apply_manifest_convenience(tmp_path: Path) -> None:
    touched = apply_manifest(tmp_path, _discord_manifest())
    assert any(p.name == "discord-post.md" for p in touched)
