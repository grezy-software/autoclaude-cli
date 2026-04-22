"""Config + profile handling for autoclaude-cli.

Config file lives at ``platformdirs.user_config_path('autoclaude') / config.toml``
and holds one or more named profiles. A profile stores the server URL, API
key, and repo checkout path. Profiles are resolvable via ``--profile`` or
``AUTOCLAUDE_PROFILE``.
"""

from __future__ import annotations

import os
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path

import tomli_w
from platformdirs import user_config_path

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover
    import tomli as tomllib

APP_NAME = "autoclaude"

BUILTIN_API_BASES: dict[str, str] = {
    "prod": "https://app.grezy.com",
    "local": "http://localhost:8000",
}
DEFAULT_PROFILE = "prod"


def config_dir() -> Path:
    return user_config_path(APP_NAME, appauthor=False)


def config_path() -> Path:
    return config_dir() / "config.toml"


@dataclass
class Profile:
    name: str = DEFAULT_PROFILE
    api_base: str = BUILTIN_API_BASES[DEFAULT_PROFILE]
    api_key: str = ""
    repo_checkout: str = ""


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
                api_base=raw.get("api_base", BUILTIN_API_BASES.get(name, "")),
                api_key=raw.get("api_key", ""),
                repo_checkout=raw.get("repo_checkout", ""),
            )
        return cls(active=active, profiles=profiles)

    def save(self) -> None:
        path = config_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "active": self.active,
            "profiles": {
                name: {k: v for k, v in asdict(profile).items() if k != "name"}
                for name, profile in self.profiles.items()
            },
        }
        with path.open("wb") as handle:
            tomli_w.dump(data, handle)

    def resolve(self, profile_flag: str | None) -> Profile:
        name = profile_flag or os.environ.get("AUTOCLAUDE_PROFILE") or self.active or DEFAULT_PROFILE
        profile = self.profiles.get(name)
        if profile is None:
            profile = Profile(name=name, api_base=BUILTIN_API_BASES.get(name, ""))
            self.profiles[name] = profile
        overrides = {
            "api_base": os.environ.get("AUTOCLAUDE_API_BASE", "").strip(),
            "api_key": os.environ.get("AUTOCLAUDE_API_KEY", "").strip(),
        }
        if overrides["api_base"]:
            profile.api_base = overrides["api_base"]
        if overrides["api_key"]:
            profile.api_key = overrides["api_key"]
        return profile


__all__ = ["APP_NAME", "BUILTIN_API_BASES", "Config", "DEFAULT_PROFILE", "Profile", "config_dir", "config_path"]
