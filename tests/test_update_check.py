"""Tests for the heartbeat-driven version freshness check."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from autoclaude import update_check
from autoclaude.daemon import Daemon
from autoclaude.installation import InstallationIdentity

_parse_version = getattr(update_check, "_parse_version")  # noqa: B009
_cmp_versions = getattr(update_check, "_cmp_versions")  # noqa: B009


@pytest.fixture(autouse=True)
def _isolate_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Redirect ``state_path()`` into a per-test directory."""
    monkeypatch.setattr(update_check, "config_dir", lambda: tmp_path)
    # Strip any forcing env vars the developer might have exported.
    monkeypatch.delenv("AUTOCLAUDE_FORCE_LATEST", raising=False)
    monkeypatch.delenv("AUTOCLAUDE_FORCE_MIN", raising=False)


def test_parse_version_handles_dev_and_prerelease() -> None:
    assert _parse_version("1.15.0") == (1, 15, 0)
    assert _parse_version("1.15.0+dev") == (1, 15, 0)
    assert _parse_version("2.0.0a1") == (2, 0, 0)
    assert _parse_version("0.0.0+dev") == (0, 0, 0)
    assert _parse_version("") == (0,)


def test_cmp_versions() -> None:
    assert _cmp_versions("1.0.0", "1.0.1") == -1
    assert _cmp_versions("1.2.0", "1.10.0") == -1  # lexical trap
    assert _cmp_versions("2.0.0", "2.0.0") == 0
    assert _cmp_versions("2.1.0", "2.0.9") == 1


def test_apply_heartbeat_persists_state(tmp_path: Path) -> None:
    response = {"latest_version": "2.0.0", "min_version": "1.5.0"}
    status = update_check.apply_heartbeat_response(response, current="1.10.0")
    assert status.outdated is True
    assert status.blocking is False
    state = json.loads((tmp_path / update_check.STATE_FILENAME).read_text())
    assert state["latest_version"] == "2.0.0"
    assert state["min_version"] == "1.5.0"
    assert state["checked_at"]


def test_apply_heartbeat_blocking_when_below_min() -> None:
    response = {"latest_version": "2.0.0", "min_version": "2.0.0"}
    status = update_check.apply_heartbeat_response(response, current="1.0.0")
    assert status.blocking is True
    assert status.outdated is True


def test_apply_heartbeat_dev_build_never_outdated() -> None:
    response = {"latest_version": "99.0.0", "min_version": "50.0.0"}
    status = update_check.apply_heartbeat_response(response, current="0.0.0+dev")
    assert status.outdated is False
    assert status.blocking is False


def test_apply_heartbeat_tolerates_missing_fields() -> None:
    status = update_check.apply_heartbeat_response({}, current="1.0.0")
    assert status.outdated is False
    assert status.blocking is False
    status = update_check.apply_heartbeat_response("not a dict", current="1.0.0")
    assert status.outdated is False


def test_env_overrides_response(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AUTOCLAUDE_FORCE_LATEST", "9.9.9")
    monkeypatch.setenv("AUTOCLAUDE_FORCE_MIN", "5.0.0")
    status = update_check.apply_heartbeat_response(
        {"latest_version": "1.0.0", "min_version": "0.1.0"},
        current="1.0.0",
    )
    assert status.latest == "9.9.9"
    assert status.minimum == "5.0.0"
    assert status.outdated is True
    assert status.blocking is True


def test_maybe_notify_dedupes_per_latest_version(monkeypatch: pytest.MonkeyPatch) -> None:
    fired: list[tuple[str, str]] = []

    def _fake_notify(title: str, body: str) -> bool:
        fired.append((title, body))
        return True

    monkeypatch.setattr(update_check, "_native_notify", _fake_notify)

    status = update_check.apply_heartbeat_response(
        {"latest_version": "2.0.0", "min_version": "1.0.0"},
        current="1.5.0",
    )
    assert update_check.maybe_notify(status) is True
    # Re-load + re-check: same latest, so no second notification.
    status2 = update_check.apply_heartbeat_response(
        {"latest_version": "2.0.0", "min_version": "1.0.0"},
        current="1.5.0",
    )
    assert update_check.maybe_notify(status2) is False
    # New latest -> notification fires again.
    status3 = update_check.apply_heartbeat_response(
        {"latest_version": "2.1.0", "min_version": "1.0.0"},
        current="1.5.0",
    )
    assert update_check.maybe_notify(status3) is True
    assert len(fired) == 2


def test_maybe_notify_blocking_separate_dedupe(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(update_check, "_native_notify", lambda *_: True)
    status = update_check.apply_heartbeat_response(
        {"latest_version": "2.0.0", "min_version": "2.0.0"},
        current="1.0.0",
    )
    assert update_check.maybe_notify(status) is True
    status2 = update_check.apply_heartbeat_response(
        {"latest_version": "2.0.0", "min_version": "2.0.0"},
        current="1.0.0",
    )
    assert update_check.maybe_notify(status2) is False


def test_load_status_without_state_returns_clean() -> None:
    status = update_check.load_status()
    assert status.latest == ""
    assert status.minimum == ""
    assert status.outdated is False
    assert status.blocking is False


def test_format_console_notice_messages() -> None:
    status_outdated = update_check.UpdateStatus(
        current="1.0.0",
        latest="2.0.0",
        minimum="0.5.0",
        outdated=True,
        blocking=False,
        state=update_check.UpdateState(),
    )
    notice = update_check.format_console_notice(status_outdated)
    assert notice is not None
    assert "2.0.0" in notice and "1.0.0" in notice

    status_blocking = update_check.UpdateStatus(
        current="1.0.0",
        latest="2.0.0",
        minimum="2.0.0",
        outdated=True,
        blocking=True,
        state=update_check.UpdateState(),
    )
    blocking_notice = update_check.format_console_notice(status_blocking)
    assert blocking_notice is not None
    assert "requires" in blocking_notice.lower()

    status_ok = update_check.UpdateStatus(
        current="2.0.0",
        latest="2.0.0",
        minimum="1.0.0",
        outdated=False,
        blocking=False,
        state=update_check.UpdateState(),
    )
    assert update_check.format_console_notice(status_ok) is None


def test_clear_state_removes_file() -> None:
    update_check.apply_heartbeat_response({"latest_version": "1.0.0"}, current="1.0.0")
    assert update_check.state_path().exists()
    assert update_check.clear_state() is True
    assert not update_check.state_path().exists()
    assert update_check.clear_state() is False


def _make_response(*, latest: str = "", minimum: str = "") -> dict[str, Any]:
    return {"ok": True, "tasks": [], "latest_version": latest, "min_version": minimum}


def test_daemon_hard_stops_when_blocking(monkeypatch: pytest.MonkeyPatch) -> None:
    """``_tick_once`` must raise SystemExit(2) when below ``min_version``."""

    class _Client:
        def heartbeat(self, **_: Any) -> dict[str, Any]:
            return _make_response(latest="9.0.0", minimum="9.0.0")

    monkeypatch.setattr(update_check, "_native_notify", lambda *_: True)
    daemon = Daemon(
        _Client(),
        cli_version="1.0.0",
        interval=0.01,
        identity=InstallationIdentity(installation_id="x", hostname="h", os_platform="linux"),
    )
    with pytest.raises(SystemExit) as exc:
        daemon._tick_once()  # noqa: SLF001
    assert exc.value.code == 2
    assert daemon._stop.is_set()  # noqa: SLF001
