"""Tests for the persisted installation identity used by the daemon."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

from autoclaude import installation


def test_get_or_create_identity_persists_uuid(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(installation, "config_dir", lambda: tmp_path)
    first = installation.get_or_create_identity()
    second = installation.get_or_create_identity()
    assert first.installation_id == second.installation_id
    stored = json.loads((tmp_path / installation.INSTALLATION_FILENAME).read_text())
    assert stored["installation_id"] == first.installation_id


def test_get_or_create_identity_repairs_corrupt_file(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(installation, "config_dir", lambda: tmp_path)
    (tmp_path / installation.INSTALLATION_FILENAME).write_text("not json")
    identity = installation.get_or_create_identity()
    assert identity.installation_id  # regenerated, not empty
    refreshed = json.loads((tmp_path / installation.INSTALLATION_FILENAME).read_text())
    assert refreshed["installation_id"] == identity.installation_id


def test_get_or_create_identity_includes_hostname_and_platform(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(installation, "config_dir", lambda: tmp_path)
    with patch.object(installation, "_detect_hostname", return_value="laptop.local"), patch.object(
        installation.sys,
        "platform",
        "darwin",
    ):
        identity = installation.get_or_create_identity()
    assert identity.hostname == "laptop.local"
    assert identity.os_platform == "darwin"
