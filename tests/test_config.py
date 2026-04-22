from __future__ import annotations

import os

from autoclaude.config import DEFAULT_PROFILE, DEFAULT_URL, Config


def test_resolve_returns_default_url_when_missing(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    cfg = Config()
    prof = cfg.resolve(None)
    assert prof.url == DEFAULT_URL
    assert prof.name == DEFAULT_PROFILE


def test_resolve_respects_env_overrides(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.setenv("AUTOCLAUDE_URL", "https://other.example")
    monkeypatch.setenv("AUTOCLAUDE_API_KEY", "xxx")
    cfg = Config()
    prof = cfg.resolve(None)
    assert prof.url == "https://other.example"
    assert prof.api_key == "xxx"
    assert prof.name == DEFAULT_PROFILE


def test_save_and_load_roundtrip(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.delenv("AUTOCLAUDE_URL", raising=False)
    monkeypatch.delenv("AUTOCLAUDE_API_KEY", raising=False)
    cfg = Config()
    prof = cfg.resolve("staging")
    prof.url = "http://localhost:3001"
    prof.api_key = "secret"
    cfg.profiles[prof.name] = prof
    cfg.active = prof.name
    cfg.save()

    reloaded = Config.load()
    assert reloaded.active == "staging"
    assert reloaded.profiles["staging"].api_key == "secret"
    assert reloaded.profiles["staging"].url == "http://localhost:3001"

    os.environ.pop("AUTOCLAUDE_URL", None)


def test_load_migrates_legacy_api_base(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    path = tmp_path / "autoclaude" / "config.toml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        '[profiles.prod]\napi_base = "https://legacy.example"\napi_key = "k"\n',
        encoding="utf-8",
    )
    reloaded = Config.load()
    assert reloaded.profiles["prod"].url == "https://legacy.example"
