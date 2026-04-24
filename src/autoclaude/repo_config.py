"""Per-repo, committed ``.autoclaude/config.toml``.

Holds three kinds of settings:

- ``name`` -- a stable repo identifier used in server reports. Avoids
  mis-routing when the checkout path is ambiguous (worktrees, forks).
- ``[claude]`` -- overrides for the Claude CLI spawn (model, extra args).
- ``[retention]`` -- how long the CLI keeps logs / reports / cached docs
  before :meth:`RepoStorage.prune` deletes them.

Loading is defensive: missing file -> defaults. Unknown keys are tolerated
so the schema can grow without breaking older clients. Writes only happen
via :func:`scaffold_default`, which is invoked by ``autoclaude init`` (never
by the auto-heal path).
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from autoclaude.storage import ROOT_NAME

CONFIG_FILE = "config.toml"

_DEFAULT_LOGS_DAYS = 14
_DEFAULT_REPORTS_DAYS = 30
_DEFAULT_API_DOCS_DAYS = 7

_DEFAULT_TEMPLATE = """\
# .autoclaude/config.toml
#
# Committed per-repo settings for autoclaude-cli. Safe to hand-edit.
# All keys are optional; unknown keys are ignored so future CLI versions
# remain backwards compatible.

# Stable identifier for this repo. Used in server-side reports. Defaults
# to the repo directory name when left blank.
name = ""

[claude]
# Pin a Claude model for ticks in this repo. Leave empty to inherit the
# CLI default (whatever the `claude` binary picks).
model = ""

# Extra arguments forwarded to every `claude -p` invocation.
extra_args = []

[retention]
# How many days of per-tick logs, failure reports, and cached API docs
# to keep on disk. Zero disables pruning for that category.
logs_days = 14
reports_days = 30
api_docs_days = 7
"""


@dataclass(frozen=True)
class Retention:
    logs_days: int = _DEFAULT_LOGS_DAYS
    reports_days: int = _DEFAULT_REPORTS_DAYS
    api_docs_days: int = _DEFAULT_API_DOCS_DAYS


@dataclass(frozen=True)
class RepoConfig:
    name: str = ""
    claude_model: str = ""
    claude_extra_args: list[str] = field(default_factory=list)
    retention: Retention = field(default_factory=Retention)


def config_path(repo_root: Path) -> Path:
    return repo_root / ROOT_NAME / CONFIG_FILE


def load(repo_root: Path) -> RepoConfig:
    """Read ``<repo>/.autoclaude/config.toml`` or return defaults if absent."""
    path = config_path(repo_root)
    if not path.exists():
        return RepoConfig()
    try:
        with path.open("rb") as handle:
            data = tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError):
        return RepoConfig()
    return _from_dict(data)


def scaffold_default(repo_root: Path) -> Path:
    """Write the default ``config.toml`` if it does not already exist."""
    path = config_path(repo_root)
    if path.exists():
        return path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_DEFAULT_TEMPLATE, encoding="utf-8")
    return path


def _from_dict(data: dict[str, object]) -> RepoConfig:
    name_raw = data.get("name", "")
    name = name_raw if isinstance(name_raw, str) else ""
    claude_raw = data.get("claude") if isinstance(data.get("claude"), dict) else {}
    model_raw = claude_raw.get("model", "") if isinstance(claude_raw, dict) else ""
    model = model_raw if isinstance(model_raw, str) else ""
    extra_raw = claude_raw.get("extra_args", []) if isinstance(claude_raw, dict) else []
    extra = [str(x) for x in extra_raw] if isinstance(extra_raw, list) else []
    retention_raw = data.get("retention") if isinstance(data.get("retention"), dict) else {}
    retention = _retention_from_dict(retention_raw if isinstance(retention_raw, dict) else {})
    return RepoConfig(
        name=name,
        claude_model=model,
        claude_extra_args=extra,
        retention=retention,
    )


def _retention_from_dict(raw: dict[str, object]) -> Retention:
    def _as_int(key: str, default: int) -> int:
        value = raw.get(key, default)
        if isinstance(value, bool):  # bool is a subclass of int; reject explicitly
            return default
        if isinstance(value, int):
            return max(0, value)
        return default

    return Retention(
        logs_days=_as_int("logs_days", _DEFAULT_LOGS_DAYS),
        reports_days=_as_int("reports_days", _DEFAULT_REPORTS_DAYS),
        api_docs_days=_as_int("api_docs_days", _DEFAULT_API_DOCS_DAYS),
    )


__all__ = [
    "CONFIG_FILE",
    "RepoConfig",
    "Retention",
    "config_path",
    "load",
    "scaffold_default",
]
