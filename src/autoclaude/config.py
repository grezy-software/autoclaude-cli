"""Config + profile handling for autoclaude-cli.

Config lives at ``platformdirs.user_config_path('autoclaude') / config.toml``.
A profile stores one URL (serving both the API and the frontend), the API key,
and an optional repo checkout path. Profiles are resolvable via ``--profile``
or ``AUTOCLAUDE_PROFILE``.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import asdict, dataclass, field
from pathlib import Path

import tomli_w
from platformdirs import user_config_path

APP_NAME = "autoclaude"

DEFAULT_URL = "https://app.grezy.com"
DEFAULT_PROFILE = "default"


def config_dir() -> Path:
    return user_config_path(APP_NAME, appauthor=False)


def config_path() -> Path:
    return config_dir() / "config.toml"


@dataclass
class Profile:
    name: str = DEFAULT_PROFILE
    url: str = DEFAULT_URL
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
                url=raw.get("url") or raw.get("api_base") or DEFAULT_URL,
                api_key=raw.get("api_key", ""),
                repo_checkout=raw.get("repo_checkout", ""),
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
