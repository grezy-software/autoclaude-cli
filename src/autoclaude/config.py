"""Config + profile handling for autoclaude-cli.

Config lives at ``$XDG_CONFIG_HOME/autoclaude/config.toml`` (default
``~/.config/autoclaude/config.toml``). A profile stores one URL (serving
both the API and the frontend), the API key, and an optional repo
checkout path. Profiles are resolvable via ``--profile`` or
``AUTOCLAUDE_PROFILE``.
"""

from __future__ import annotations

import os
import shutil
import tomllib
from dataclasses import asdict, dataclass, field
from pathlib import Path

import tomli_w

APP_NAME = "autoclaude"

DEFAULT_URL = "https://autoclaude.grezy.org"
DEFAULT_PROFILE = "default"


def config_dir() -> Path:
    """Return the CLI's config directory.

    Honours ``XDG_CONFIG_HOME`` and otherwise lands at ``~/.config/autoclaude``.

    Why: ``platformdirs.user_config_path`` returns ``~/Library/Application
    Support/autoclaude`` on macOS. Any access to that path from an
    unbundled Python (``uv tool install``) triggers the macOS TCC "would
    like to access data from other apps" prompt on every tick. A plain
    dotfolder in ``$HOME`` is not TCC-gated, so keeping config there
    silences the prompt on macOS without changing behaviour on Linux.
    """
    xdg = os.environ.get("XDG_CONFIG_HOME")
    base = Path(xdg) if xdg else Path.home() / ".config"
    return base / APP_NAME


def _legacy_macos_config_dir() -> Path:
    """Where platformdirs<5 put macOS config before the TCC fix."""
    return Path.home() / "Library" / "Application Support" / APP_NAME


def _migrate_legacy_macos_config(target: Path) -> None:
    """Move data out of ``~/Library/Application Support/autoclaude`` once.

    No-op unless the legacy directory exists and the new one does not.
    Tolerates partial copies (errors are swallowed) so a migration failure
    never blocks the CLI from running against the new path.
    """
    legacy = _legacy_macos_config_dir()
    if target.exists() or not legacy.exists():
        return
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(legacy), str(target))
    except OSError:
        # Best-effort migration; the CLI will simply start fresh at the new
        # path if the move fails (e.g. permissions). The user can remove the
        # old directory themselves afterwards.
        pass


def config_path() -> Path:
    target = config_dir()
    _migrate_legacy_macos_config(target)
    return target / "config.toml"


@dataclass
class Profile:
    name: str = DEFAULT_PROFILE
    url: str = DEFAULT_URL
    api_key: str = ""
    repo_checkout: str = ""
    autoclaude_root: str = ""
    paused: bool = False

    def resolve_autoclaude_root(self: Profile) -> Path:
        """Where the CLI stores cached docs, reports, and attempt state.

        Defaults to the shared workspace home so these client-wide
        artefacts (doc cache, protocol stage tracker, failure reports) do
        not leak into whatever directory ``autoclaude`` happens to be
        invoked from. Imported lazily to avoid a circular import with
        :mod:`autoclaude.workspace`.
        """
        if self.autoclaude_root:
            return Path(self.autoclaude_root).expanduser()
        from autoclaude.workspace import workspace_home  # noqa: PLC0415

        return workspace_home() / "client"


@dataclass
class Config:
    active: str = DEFAULT_PROFILE
    profiles: dict[str, Profile] = field(default_factory=dict)

    @classmethod
    def load(cls) -> Config:
        path = config_path()
        if not path.exists():
            return cls(active=DEFAULT_PROFILE, profiles={})
        with path.open("rb") as handle:
            data = tomllib.load(handle)
        active = data.get("active", DEFAULT_PROFILE)
        profiles: dict[str, Profile] = {}
        for name, raw in (data.get("profiles") or {}).items():
            profiles[name] = Profile(
                name=name,
                url=raw.get("url") or raw.get("api_base") or DEFAULT_URL,
                api_key=raw.get("api_key", ""),
                repo_checkout=raw.get("repo_checkout", ""),
                autoclaude_root=raw.get("autoclaude_root", ""),
                paused=bool(raw.get("paused", False)),
            )
        return cls(active=active, profiles=profiles)

    def save(self) -> None:
        path = config_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "active": self.active,
            "profiles": {name: {k: v for k, v in asdict(profile).items() if k != "name"} for name, profile in self.profiles.items()},
        }
        with path.open("wb") as handle:
            tomli_w.dump(data, handle)

    def resolve(self, profile_flag: str | None) -> Profile:
        name = profile_flag or os.environ.get("AUTOCLAUDE_PROFILE") or self.active or DEFAULT_PROFILE
        profile = self.profiles.get(name)
        if profile is None:
            profile = Profile(name=name)
            self.profiles[name] = profile
        url_override = os.environ.get("AUTOCLAUDE_URL", "").strip()
        api_key_override = os.environ.get("AUTOCLAUDE_API_KEY", "").strip()
        if url_override:
            profile.url = url_override
        if api_key_override:
            profile.api_key = api_key_override
        return profile


__all__ = [
    "APP_NAME",
    "DEFAULT_PROFILE",
    "DEFAULT_URL",
    "Config",
    "Profile",
    "config_dir",
    "config_path",
]
