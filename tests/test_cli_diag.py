"""Tests for the ``diag`` command's profiles ping block.

The diag command exposes a ``profiles:`` block listing every configured
profile with a ping result next to its name. These tests pin that
contract so the layout and per-profile dispatch stay stable across
refactors.
"""

from __future__ import annotations

import inspect
import logging
from collections.abc import Iterator
from typing import Any, Self

import pytest

from autoclaude import cli
from autoclaude.api_client import ApiError
from autoclaude.config import Config, Profile

# autoclaude.logger sets ``propagate=False`` so pytest's caplog never sees the
# diag output. Tests attach a Handler directly to the autoclaude.cli child
# logger to capture records, mirroring the pattern in test_claude_proc.


class _CaptureHandler(logging.Handler):
    """In-memory handler used by the diag tests to assert log content."""

    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)

    def messages(self) -> list[str]:
        return [r.getMessage() for r in self.records]


@pytest.fixture()
def capture_cli_logs() -> Iterator[_CaptureHandler]:
    capture = _CaptureHandler()
    logger = logging.getLogger("autoclaude.cli")
    logger.addHandler(capture)
    try:
        yield capture
    finally:
        logger.removeHandler(capture)


_OWNERS: dict[str, Any] = {}
_INSTANCES: list[_StubApiClient] = []


class _StubApiClient:
    """Minimal ApiClient stand-in: returns a canned context (or raises ApiError)."""

    def __init__(self, profile: Profile, *_args: object, **_kwargs: object) -> None:
        self.profile = profile
        _INSTANCES.append(self)

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_exc: object) -> None:
        return None

    def context(self) -> dict[str, Any]:
        owner = _OWNERS.get(self.profile.name)
        if isinstance(owner, ApiError):
            raise owner
        return {"owner": owner}


@pytest.fixture(autouse=True)
def _reset_stub_state() -> None:
    _INSTANCES.clear()
    _OWNERS.clear()


@pytest.fixture()
def _patch_api_client(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli, "ApiClient", _StubApiClient)


def _profile_records(capture: _CaptureHandler) -> list[logging.LogRecord]:
    """Return only the records emitted from inside the per-profile ``profile_context``.

    The diag block emits the ``profiles:`` header at profile=``-`` and
    each per-profile line at profile=``<name>``; tests filter on the
    ``profile`` extra to assert which line belongs to which profile.
    """
    return [r for r in capture.records if getattr(r, "profile", "-") not in {"-", ""}]


def test_profiles_block_lists_every_configured_profile_in_sorted_order(
    capture_cli_logs: _CaptureHandler,
    _patch_api_client: None,
) -> None:
    """Each configured profile must produce a line; order is deterministic (sorted)."""
    _OWNERS["alpha"] = {"email": "a@example.com"}
    _OWNERS["bravo"] = {"email": "b@example.com"}
    cfg = Config(
        active="alpha",
        profiles={
            "bravo": Profile(name="bravo", url="https://b.example.com", api_key="kb"),
            "alpha": Profile(name="alpha", url="https://a.example.com", api_key="ka"),
        },
    )
    cli._report_profiles_ping(cfg)  # noqa: SLF001
    msgs = capture_cli_logs.messages()
    assert any(m == "profiles:" for m in msgs)
    # Ordering and per-profile context come from the ``profile`` LogRecord
    # attribute injected by ``profile_context``.
    profile_tags = [r.profile for r in _profile_records(capture_cli_logs)]
    assert profile_tags == ["alpha", "bravo"]
    # ApiClient must be invoked once per profile.
    assert [c.profile.name for c in _INSTANCES] == ["alpha", "bravo"]


def test_ping_ok_reports_owner_label(
    capture_cli_logs: _CaptureHandler,
    _patch_api_client: None,
) -> None:
    _OWNERS["default"] = {"email": "ops@example.com"}
    prof = Profile(name="default", url="https://api.example.com", api_key="k")
    cli._ping_profile(prof)  # noqa: SLF001
    msgs = capture_cli_logs.messages()
    line = next(m for m in msgs if "ping ok" in m)
    assert "ops@example.com" in line
    assert "https://api.example.com" in line


def test_ping_failure_is_surfaced_per_profile(
    capture_cli_logs: _CaptureHandler,
    _patch_api_client: None,
) -> None:
    _OWNERS["broken"] = ApiError("HTTP 401: unauthorized")
    prof = Profile(name="broken", url="https://api.example.com", api_key="k")
    cli._ping_profile(prof)  # noqa: SLF001
    line = next(m for m in capture_cli_logs.messages() if "ping failed" in m)
    assert "HTTP 401" in line


def test_ping_skipped_when_url_missing(capture_cli_logs: _CaptureHandler) -> None:
    """Empty url means no ping attempt; surface a clear configuration error instead."""
    prof = Profile(name="incomplete", url="", api_key="k")
    cli._ping_profile(prof)  # noqa: SLF001
    msgs = capture_cli_logs.messages()
    assert any("no url configured" in m for m in msgs)
    assert _INSTANCES == []


def test_ping_skipped_when_api_key_missing(capture_cli_logs: _CaptureHandler) -> None:
    """Url without api_key cannot ping; surface configuration error instead of network call."""
    prof = Profile(name="no-key", url="https://api.example.com", api_key="")
    cli._ping_profile(prof)  # noqa: SLF001
    msgs = capture_cli_logs.messages()
    assert any("no api_key configured" in m for m in msgs)
    assert _INSTANCES == []


def test_empty_config_falls_back_to_default_profile(
    capture_cli_logs: _CaptureHandler,
    _patch_api_client: None,
) -> None:
    """First-run (no [profiles] section in config) still yields one ping line.

    The default profile carries DEFAULT_URL but no api_key, so the line
    must surface the missing-credential warning rather than crash.
    """
    cfg = Config(active="default", profiles={})
    cli._report_profiles_ping(cfg)  # noqa: SLF001
    msgs = capture_cli_logs.messages()
    assert any(m == "profiles:" for m in msgs)
    assert any("no api_key configured" in m for m in msgs)
    profile_tags = [r.profile for r in _profile_records(capture_cli_logs)]
    assert profile_tags == ["default"]


def test_diag_command_has_no_profile_option() -> None:
    """``autoclaude diag`` no longer accepts ``--profile``: the command is fleet-wide."""
    sig = inspect.signature(cli.diag)
    assert list(sig.parameters) == ["ctx"], (
        f"diag signature should only take ctx, got {list(sig.parameters)}"
    )
