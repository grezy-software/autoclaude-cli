from __future__ import annotations

import os

from autoclaude.config import BUILTIN_API_BASES, BUILTIN_FRONTEND_BASES, DEFAULT_PROFILE, Config


def test_resolve_returns_builtin_base_when_missing(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    cfg = Config()
    prof = cfg.resolve("local")
    assert prof.api_base == BUILTIN_API_BASES["local"]


def test_resolve_respects_env_overrides(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.setenv("AUTOCLAUDE_API_BASE", "https://other.example")
    monkeypatch.setenv("AUTOCLAUDE_API_KEY", "xxx")
    cfg = Config()
    prof = cfg.resolve(None)
    assert prof.api_base == "https://other.example"
    assert prof.api_key == "xxx"
    assert prof.name == DEFAULT_PROFILE


def test_save_and_load_roundtrip(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.delenv("AUTOCLAUDE_API_BASE", raising=False)
    monkeypatch.delenv("AUTOCLAUDE_API_KEY", raising=False)
    cfg = Config()
    prof = cfg.resolve("local")
    prof.api_key = "secret"
    cfg.profiles[prof.name] = prof
    cfg.active = prof.name
    cfg.save()

    reloaded = Config.load()
    assert reloaded.active == "local"
    assert reloaded.profiles["local"].api_key == "secret"
    assert reloaded.profiles["local"].api_base == BUILTIN_API_BASES["local"]

    os.environ.pop("AUTOCLAUDE_API_BASE", None)


def test_resolved_frontend_base_prefers_explicit_then_builtin_then_api(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    cfg = Config()

    # Builtin local profile: falls back to the builtin frontend URL, not api_base.
    local = cfg.resolve("local")
    assert local.resolved_frontend_base() == BUILTIN_FRONTEND_BASES["local"]

    # Explicit override wins.
    local.frontend_base = "https://custom.example"
    assert local.resolved_frontend_base() == "https://custom.example"

    # Unknown profile with only api_base falls back to it.
    custom = cfg.resolve("staging")
    custom.api_base = "https://stage.example"
    assert custom.resolved_frontend_base() == "https://stage.example"
