"""Auto-create flow: fill in `project.github_repo` on the first tick."""

from __future__ import annotations

import pytest

from autoclaude.gh import GhError
from autoclaude.runner import (
    _autocreate_github_repo,
    _find_available_repo_name,
    _slugify_for_github,
)

# --- _slugify_for_github -----------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Soaria", "soaria"),
        ("Soaria App", "soaria-app"),
        ("  Padded   Name  ", "padded-name"),
        ("Mix3d/Slash:Punct!", "mix3d-slash-punct"),
        ("---trim---", "trim"),
        ("é accents grave", "accents-grave"),  # non-ascii falls into the disallowed bucket
        ("", "autoclaude-project"),
        ("***", "autoclaude-project"),
        ("a" * 200, "a" * 100),
    ],
)
def test_slugify_for_github_normalises(raw: str, expected: str) -> None:
    assert _slugify_for_github(raw) == expected


# --- _find_available_repo_name ----------------------------------------------


def test_find_available_repo_name_returns_base_when_free(monkeypatch) -> None:
    monkeypatch.setattr("autoclaude.runner.gh_helpers.repo_exists", lambda _repo: False)
    assert _find_available_repo_name("alice", "soaria") == "soaria"


def test_find_available_repo_name_increments_until_free(monkeypatch) -> None:
    taken = {"alice/soaria", "alice/soaria-1", "alice/soaria-2"}
    monkeypatch.setattr(
        "autoclaude.runner.gh_helpers.repo_exists",
        lambda repo: repo in taken,
    )
    assert _find_available_repo_name("alice", "soaria") == "soaria-3"


def test_find_available_repo_name_raises_when_cap_exhausted(monkeypatch) -> None:
    """Pathological case where every suffix is taken; we error rather than spin forever."""
    monkeypatch.setattr("autoclaude.runner.gh_helpers.repo_exists", lambda _repo: True)
    with pytest.raises(GhError) as err:
        _find_available_repo_name("alice", "soaria", max_attempts=3)
    assert "all suffixes 0..2 are taken" in str(err.value)


# --- _autocreate_github_repo ------------------------------------------------


class _FakeClient:
    def __init__(self) -> None:
        self.calls: list[tuple[int, str]] = []

    def update_project_github_repo(self, project_id: int, github_repo: str) -> dict:
        self.calls.append((project_id, github_repo))
        return {"id": project_id, "github_repo": github_repo}


def test_autocreate_github_repo_full_flow(monkeypatch) -> None:
    """Empty github_repo -> slug -> available name -> gh repo create -> server patch."""
    monkeypatch.setattr("autoclaude.runner.gh_helpers.current_user_login", lambda: "alice")
    monkeypatch.setattr("autoclaude.runner.gh_helpers.repo_exists", lambda _repo: False)

    created: list[tuple[str, bool]] = []

    def _create(repo: str, *, private: bool) -> None:
        created.append((repo, private))

    monkeypatch.setattr("autoclaude.runner.gh_helpers.repo_create", _create)

    client = _FakeClient()
    full = _autocreate_github_repo(client, {"id": 7, "name": "Soaria App"})

    assert full == "alice/soaria-app"
    assert created == [("alice/soaria-app", True)]
    assert client.calls == [(7, "alice/soaria-app")]


def test_autocreate_github_repo_picks_first_free_suffix(monkeypatch) -> None:
    monkeypatch.setattr("autoclaude.runner.gh_helpers.current_user_login", lambda: "bob")
    taken = {"bob/widget", "bob/widget-1"}
    monkeypatch.setattr(
        "autoclaude.runner.gh_helpers.repo_exists",
        lambda repo: repo in taken,
    )
    monkeypatch.setattr("autoclaude.runner.gh_helpers.repo_create", lambda *_a, **_k: None)
    client = _FakeClient()
    full = _autocreate_github_repo(client, {"id": 11, "name": "widget"})
    assert full == "bob/widget-2"
    assert client.calls == [(11, "bob/widget-2")]


def test_autocreate_github_repo_requires_project_id(monkeypatch) -> None:
    """Without an `id`, we have nothing to PATCH back, so we refuse."""
    monkeypatch.setattr("autoclaude.runner.gh_helpers.current_user_login", lambda: "alice")
    client = _FakeClient()
    with pytest.raises(GhError):
        _autocreate_github_repo(client, {"name": "no id here"})
    assert client.calls == [], "no patch must reach the server when validation fails"


def test_autocreate_github_repo_falls_back_to_constant_name(monkeypatch) -> None:
    monkeypatch.setattr("autoclaude.runner.gh_helpers.current_user_login", lambda: "alice")
    monkeypatch.setattr("autoclaude.runner.gh_helpers.repo_exists", lambda _repo: False)
    monkeypatch.setattr("autoclaude.runner.gh_helpers.repo_create", lambda *_a, **_k: None)
    client = _FakeClient()
    full = _autocreate_github_repo(client, {"id": 1, "name": "***"})
    assert full == "alice/autoclaude-project"
