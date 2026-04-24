"""Tests for the committed per-repo `config.toml` loader."""

from __future__ import annotations

from pathlib import Path

from autoclaude import repo_config


def test_load_missing_returns_defaults(tmp_path: Path) -> None:
    cfg = repo_config.load(tmp_path)
    assert cfg.name == ""
    assert cfg.claude_model == ""
    assert cfg.claude_extra_args == []
    assert cfg.retention.logs_days == 14
    assert cfg.retention.reports_days == 30
    assert cfg.retention.api_docs_days == 7


def test_scaffold_default_is_idempotent(tmp_path: Path) -> None:
    first = repo_config.scaffold_default(tmp_path)
    original = first.read_text(encoding="utf-8")
    first.write_text(original + "\n# user comment\n", encoding="utf-8")
    second = repo_config.scaffold_default(tmp_path)
    assert second == first
    assert second.read_text(encoding="utf-8").endswith("# user comment\n")


def test_load_reads_populated_config(tmp_path: Path) -> None:
    ac_dir = tmp_path / ".autoclaude"
    ac_dir.mkdir()
    (ac_dir / "config.toml").write_text(
        """
name = "grezy/nango"

[claude]
model = "claude-opus-4-7"
extra_args = ["--verbose", "--dangerously-skip-permissions"]

[retention]
logs_days = 3
reports_days = 60
api_docs_days = 1
""",
        encoding="utf-8",
    )
    cfg = repo_config.load(tmp_path)
    assert cfg.name == "grezy/nango"
    assert cfg.claude_model == "claude-opus-4-7"
    assert cfg.claude_extra_args == ["--verbose", "--dangerously-skip-permissions"]
    assert cfg.retention.logs_days == 3
    assert cfg.retention.reports_days == 60
    assert cfg.retention.api_docs_days == 1


def test_load_tolerates_unknown_keys(tmp_path: Path) -> None:
    ac_dir = tmp_path / ".autoclaude"
    ac_dir.mkdir()
    (ac_dir / "config.toml").write_text(
        """
name = "x"
future_key = "whatever"

[claude]
model = "m"
unknown = 42

[retention]
logs_days = 5
""",
        encoding="utf-8",
    )
    cfg = repo_config.load(tmp_path)
    assert cfg.name == "x"
    assert cfg.claude_model == "m"
    assert cfg.retention.logs_days == 5
    assert cfg.retention.reports_days == 30  # default preserved


def test_load_tolerates_malformed_file(tmp_path: Path) -> None:
    ac_dir = tmp_path / ".autoclaude"
    ac_dir.mkdir()
    (ac_dir / "config.toml").write_text("this is not toml {[{", encoding="utf-8")
    cfg = repo_config.load(tmp_path)
    assert cfg == repo_config.RepoConfig()
