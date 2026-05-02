"""Tests for the claude environment / privilege-drop helpers."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from autoclaude import claude_env
from autoclaude.claude_env import (
    UserCreationError,
    _read_settings_file,
    autoclaude_subprocess_env_overrides,
    ensure_autoclaude_user,
    is_root,
    read_default_permission_mode,
    reset_caches,
    share_claude_config,
    share_claude_credentials,
    share_repo,
    share_workspace_home,
    should_bypass_permissions,
    wrap_for_user,
)


@pytest.fixture(autouse=True)
def _reset_module_caches() -> None:
    reset_caches()


def _write_settings(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def test_read_settings_file_returns_empty_on_missing(tmp_path: Path) -> None:
    assert _read_settings_file(tmp_path / "absent.json") == {}


def test_read_settings_file_returns_empty_on_bad_json(tmp_path: Path) -> None:
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    assert _read_settings_file(bad) == {}


def test_read_settings_file_returns_empty_on_non_dict_root(tmp_path: Path) -> None:
    arr = tmp_path / "arr.json"
    arr.write_text("[1, 2]", encoding="utf-8")
    assert _read_settings_file(arr) == {}


def test_read_default_permission_mode_user_settings(tmp_path: Path) -> None:
    home = tmp_path / "home"
    cwd = tmp_path / "repo"
    cwd.mkdir(parents=True)
    _write_settings(home / ".claude" / "settings.json", {"permissions": {"defaultMode": "auto"}})
    assert read_default_permission_mode(home=home, cwd=cwd) == "auto"


def test_read_default_permission_mode_project_overrides_user(tmp_path: Path) -> None:
    home = tmp_path / "home"
    cwd = tmp_path / "repo"
    cwd.mkdir(parents=True)
    _write_settings(home / ".claude" / "settings.json", {"permissions": {"defaultMode": "auto"}})
    _write_settings(cwd / ".claude" / "settings.json", {"permissions": {"defaultMode": "plan"}})
    assert read_default_permission_mode(home=home, cwd=cwd) == "plan"


def test_read_default_permission_mode_returns_none_when_absent(tmp_path: Path) -> None:
    home = tmp_path / "home"
    cwd = tmp_path / "repo"
    cwd.mkdir(parents=True)
    assert read_default_permission_mode(home=home, cwd=cwd) is None


def test_read_default_permission_mode_ignores_non_dict_permissions(tmp_path: Path) -> None:
    home = tmp_path / "home"
    cwd = tmp_path / "repo"
    cwd.mkdir(parents=True)
    _write_settings(home / ".claude" / "settings.json", {"permissions": ["allow-all"]})
    assert read_default_permission_mode(home=home, cwd=cwd) is None


def test_should_bypass_true_when_mode_not_auto(tmp_path: Path) -> None:
    home = tmp_path / "home"
    cwd = tmp_path / "repo"
    cwd.mkdir(parents=True)
    _write_settings(home / ".claude" / "settings.json", {"permissions": {"defaultMode": "plan"}})
    assert should_bypass_permissions(home=home, cwd=cwd) is True


def test_should_bypass_false_when_mode_is_auto(tmp_path: Path) -> None:
    home = tmp_path / "home"
    cwd = tmp_path / "repo"
    cwd.mkdir(parents=True)
    _write_settings(home / ".claude" / "settings.json", {"permissions": {"defaultMode": "auto"}})
    assert should_bypass_permissions(home=home, cwd=cwd) is False


def test_should_bypass_true_when_no_settings(tmp_path: Path) -> None:
    home = tmp_path / "home"
    cwd = tmp_path / "repo"
    cwd.mkdir(parents=True)
    assert should_bypass_permissions(home=home, cwd=cwd) is True


def test_is_root_reflects_geteuid(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(claude_env.os, "geteuid", lambda: 0)
    assert is_root() is True
    monkeypatch.setattr(claude_env.os, "geteuid", lambda: 1000)
    assert is_root() is False


def test_ensure_autoclaude_user_skips_when_present(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []
    monkeypatch.setattr(claude_env, "_user_exists", lambda _u: True)
    monkeypatch.setattr(claude_env, "_group_exists", lambda _g: True)
    monkeypatch.setattr(claude_env, "_root_in_group", lambda _g: True)
    monkeypatch.setattr(claude_env, "_run_cmd", lambda argv, **_kw: calls.append(argv) or True)
    ensure_autoclaude_user()
    assert calls == []


def test_ensure_autoclaude_user_creates_group_user_and_adds_root(monkeypatch: pytest.MonkeyPatch) -> None:
    state = {"user": False, "group": False, "root_in_group": False}
    monkeypatch.setattr(claude_env, "_user_exists", lambda _u: state["user"])
    monkeypatch.setattr(claude_env, "_group_exists", lambda _g: state["group"])
    monkeypatch.setattr(claude_env, "_root_in_group", lambda _g: state["root_in_group"])

    calls: list[list[str]] = []

    def _fake_run(argv: list[str], **_kw: object) -> bool:
        calls.append(argv)
        if argv[0] == "groupadd":
            state["group"] = True
        if argv[0] == "useradd":
            state["user"] = True
        if argv[0] == "usermod":
            state["root_in_group"] = True
        return True

    monkeypatch.setattr(claude_env, "_run_cmd", _fake_run)

    ensure_autoclaude_user()

    issued = [c[0] for c in calls]
    assert "groupadd" in issued
    assert "useradd" in issued
    assert "usermod" in issued


def test_ensure_autoclaude_user_falls_back_to_adduser(monkeypatch: pytest.MonkeyPatch) -> None:
    state = {"user": False, "group": True, "root_in_group": True}
    monkeypatch.setattr(claude_env, "_user_exists", lambda _u: state["user"])
    monkeypatch.setattr(claude_env, "_group_exists", lambda _g: state["group"])
    monkeypatch.setattr(claude_env, "_root_in_group", lambda _g: state["root_in_group"])

    issued: list[list[str]] = []

    def _fake_run(argv: list[str], **_kw: object) -> bool:
        issued.append(argv)
        if argv[0] == "useradd":
            return False
        if argv[0] == "adduser":
            state["user"] = True
            return True
        return True

    monkeypatch.setattr(claude_env, "_run_cmd", _fake_run)
    ensure_autoclaude_user()
    cmds = [c[0] for c in issued]
    assert cmds == ["useradd", "adduser"]


def test_ensure_autoclaude_user_raises_when_no_tooling(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(claude_env, "_user_exists", lambda _u: True)
    monkeypatch.setattr(claude_env, "_group_exists", lambda _g: False)
    monkeypatch.setattr(claude_env, "_root_in_group", lambda _g: True)
    monkeypatch.setattr(claude_env, "_run_cmd", lambda *_a, **_k: False)
    with pytest.raises(UserCreationError) as excinfo:
        ensure_autoclaude_user()
    assert "github.com/grezy-software/autoclaude-cli/issues" in str(excinfo.value)


def test_ensure_autoclaude_user_raises_when_user_creation_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(claude_env, "_user_exists", lambda _u: False)
    monkeypatch.setattr(claude_env, "_group_exists", lambda _g: True)
    monkeypatch.setattr(claude_env, "_root_in_group", lambda _g: True)
    monkeypatch.setattr(claude_env, "_run_cmd", lambda *_a, **_k: False)
    with pytest.raises(UserCreationError) as excinfo:
        ensure_autoclaude_user()
    assert "issue" in str(excinfo.value).lower()


class _FakePwEntry:
    def __init__(self, home: Path) -> None:
        self.pw_dir = str(home)


def test_share_claude_config_symlinks_and_chgrps(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = tmp_path / "root"
    home.mkdir()
    src = home / ".claude"
    src.mkdir()
    (src / "settings.json").write_text("{}", encoding="utf-8")

    autoclaude_home = tmp_path / "home" / "autoclaude"
    monkeypatch.setattr(claude_env.pwd, "getpwnam", lambda _u: _FakePwEntry(autoclaude_home))
    invoked: list[list[str]] = []
    monkeypatch.setattr(claude_env, "_run_cmd", lambda argv, **_kw: invoked.append(argv) or True)

    share_claude_config(home=home)

    target = autoclaude_home / ".claude"
    assert target.is_symlink()
    assert target.resolve() == src.resolve()
    cmds = [c[0] for c in invoked]
    assert "chgrp" in cmds
    assert "chmod" in cmds


def test_share_claude_config_is_cached(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = tmp_path / "root"
    home.mkdir()
    (home / ".claude").mkdir()
    monkeypatch.setattr(claude_env.pwd, "getpwnam", lambda _u: _FakePwEntry(tmp_path / "home" / "autoclaude"))
    invoked: list[list[str]] = []
    monkeypatch.setattr(claude_env, "_run_cmd", lambda argv, **_kw: invoked.append(argv) or True)

    share_claude_config(home=home)
    first_call_count = len(invoked)
    share_claude_config(home=home)
    assert len(invoked) == first_call_count


def test_share_claude_config_handles_missing_source(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = tmp_path / "root"
    home.mkdir()
    invoked: list[list[str]] = []
    monkeypatch.setattr(claude_env, "_run_cmd", lambda argv, **_kw: invoked.append(argv) or True)
    share_claude_config(home=home)
    assert invoked == []


def test_share_claude_credentials_chgrps_and_chmods(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = tmp_path / "root"
    creds = home / ".claude" / claude_env.CREDENTIALS_FILENAME
    creds.parent.mkdir(parents=True)
    creds.write_text("{}", encoding="utf-8")
    invoked: list[list[str]] = []
    monkeypatch.setattr(claude_env, "_run_cmd", lambda argv, **_kw: invoked.append(argv) or True)

    share_claude_credentials(home=home)

    assert invoked == [
        ["chgrp", claude_env.AUTOCLAUDE_GROUP, str(creds)],
        ["chmod", "g+r", str(creds)],
    ]


def test_share_claude_credentials_runs_every_call(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = tmp_path / "root"
    creds = home / ".claude" / claude_env.CREDENTIALS_FILENAME
    creds.parent.mkdir(parents=True)
    creds.write_text("{}", encoding="utf-8")
    invoked: list[list[str]] = []
    monkeypatch.setattr(claude_env, "_run_cmd", lambda argv, **_kw: invoked.append(argv) or True)

    share_claude_credentials(home=home)
    first = len(invoked)
    share_claude_credentials(home=home)

    assert len(invoked) == 2 * first, "share_claude_credentials must NOT be cached: claude rewrites the file"


def test_autoclaude_subprocess_env_overrides_uses_pwd_home(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(claude_env.pwd, "getpwnam", lambda _u: _FakePwEntry(Path("/home/autoclaude")))
    overrides = autoclaude_subprocess_env_overrides()
    assert overrides == {"HOME": "/home/autoclaude"}


def test_autoclaude_subprocess_env_overrides_falls_back_when_user_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    def _missing(_u: str) -> object:
        raise KeyError("autoclaude")

    monkeypatch.setattr(claude_env.pwd, "getpwnam", _missing)
    overrides = autoclaude_subprocess_env_overrides(username="autoclaude")
    assert overrides == {"HOME": "/home/autoclaude"}


def test_share_claude_credentials_no_op_when_file_missing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = tmp_path / "root"
    (home / ".claude").mkdir(parents=True)
    invoked: list[list[str]] = []
    monkeypatch.setattr(claude_env, "_run_cmd", lambda argv, **_kw: invoked.append(argv) or True)

    share_claude_credentials(home=home)

    assert invoked == []


def test_share_gh_config_chgrps_and_symlinks(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = tmp_path / "root"
    gh_dir = home / ".config" / "gh"
    gh_dir.mkdir(parents=True)
    (gh_dir / "hosts.yml").write_text("github.com: {}", encoding="utf-8")
    autoclaude_home = tmp_path / "home" / "autoclaude"
    autoclaude_home.mkdir(parents=True)
    invoked: list[list[str]] = []
    monkeypatch.setattr(claude_env, "_run_cmd", lambda argv, **_kw: invoked.append(argv) or True)
    monkeypatch.setattr(claude_env, "_grant_path_traversal", lambda *_a, **_kw: None)
    monkeypatch.setattr(claude_env.pwd, "getpwnam", lambda _u: _FakePwEntry(autoclaude_home))

    claude_env.share_gh_config(home=home)

    target = autoclaude_home / ".config" / "gh"
    assert target.is_symlink()
    assert target.resolve() == gh_dir.resolve()
    assert ["chgrp", "-R", claude_env.AUTOCLAUDE_GROUP, str(gh_dir)] in invoked
    assert ["chmod", "-R", "g+rwX", str(gh_dir)] in invoked


def test_share_gh_config_no_op_when_dir_missing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = tmp_path / "root"
    home.mkdir()
    invoked: list[list[str]] = []
    monkeypatch.setattr(claude_env, "_run_cmd", lambda argv, **_kw: invoked.append(argv) or True)
    claude_env.share_gh_config(home=home)
    assert invoked == []


def test_share_workspace_home_chgrps_chmods_and_setgids(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = tmp_path / "root"
    home.mkdir()
    invoked: list[list[str]] = []
    monkeypatch.setattr(claude_env, "_run_cmd", lambda argv, **_kw: invoked.append(argv) or True)
    monkeypatch.setattr(claude_env, "_grant_path_traversal", lambda *_a, **_kw: None)

    share_workspace_home(home=home)

    workspace = home / ".autoclaude"
    assert workspace.exists()
    assert workspace.is_dir()
    assert ["chgrp", "-R", claude_env.AUTOCLAUDE_GROUP, str(workspace)] in invoked
    assert ["chmod", "-R", "g+rwX", str(workspace)] in invoked
    assert [
        "find",
        str(workspace),
        "-type",
        "d",
        "-exec",
        "chmod",
        "g+s",
        "{}",
        "+",
    ] in invoked


def test_share_workspace_home_creates_dir_when_missing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The setgid bit must be applied *before* any clone lands in the workspace."""
    home = tmp_path / "root"
    home.mkdir()
    monkeypatch.setattr(claude_env, "_run_cmd", lambda *_a, **_kw: True)
    monkeypatch.setattr(claude_env, "_grant_path_traversal", lambda *_a, **_kw: None)

    workspace = home / ".autoclaude"
    assert not workspace.exists()
    share_workspace_home(home=home)
    assert workspace.is_dir()


def test_share_workspace_home_is_cached(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = tmp_path / "root"
    home.mkdir()
    invoked: list[list[str]] = []
    monkeypatch.setattr(claude_env, "_run_cmd", lambda argv, **_kw: invoked.append(argv) or True)
    monkeypatch.setattr(claude_env, "_grant_path_traversal", lambda *_a, **_kw: None)

    share_workspace_home(home=home)
    first = len(invoked)
    share_workspace_home(home=home)
    assert len(invoked) == first, "share_workspace_home must be cached per-process"


def test_share_per_tick_for_autoclaude_user_calls_per_tick_helpers(monkeypatch: pytest.MonkeyPatch) -> None:
    """The per-tick entry point must call credentials + gh + repo, never install-time helpers."""
    calls: list[str] = []
    monkeypatch.setattr(claude_env, "share_claude_credentials", lambda *_a, **_kw: calls.append("creds"))
    monkeypatch.setattr(claude_env, "share_gh_config", lambda *_a, **_kw: calls.append("gh"))
    monkeypatch.setattr(claude_env, "share_repo", lambda *_a, **_kw: calls.append("repo"))

    def _must_not_run(*_a: object, **_kw: object) -> None:
        raise AssertionError("install-time helper called from per-tick path")

    monkeypatch.setattr(claude_env, "share_claude_config", _must_not_run)
    monkeypatch.setattr(claude_env, "share_claude_binary", _must_not_run)
    monkeypatch.setattr(claude_env, "share_workspace_home", _must_not_run)

    claude_env.share_per_tick_for_autoclaude_user(cwd=Path("/tmp/repo"))
    assert calls == ["creds", "gh", "repo"]

    calls.clear()
    claude_env.share_per_tick_for_autoclaude_user()
    assert calls == ["creds", "gh"], "no cwd -> share_repo skipped"


def test_share_repo_chgrps_once_per_cwd(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    invoked: list[list[str]] = []
    monkeypatch.setattr(claude_env, "_run_cmd", lambda argv, **_kw: invoked.append(argv) or True)
    share_repo(repo)
    first = len(invoked)
    share_repo(repo)
    assert len(invoked) == first


def test_wrap_for_user_uses_runuser(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(claude_env.shutil, "which", lambda name: "/usr/bin/runuser" if name == "runuser" else None)
    monkeypatch.setattr(claude_env, "_resolve_home", lambda _u: "/home/autoclaude")
    wrapped = wrap_for_user(["claude", "-p", "hi"])
    assert wrapped[:5] == ["runuser", "-u", "autoclaude", "--preserve-environment", "--"]
    assert wrapped[5:] == ["env", "HOME=/home/autoclaude", "claude", "-p", "hi"]


def test_wrap_for_user_falls_back_to_sudo(monkeypatch: pytest.MonkeyPatch) -> None:
    def _which(name: str) -> str | None:
        if name == "sudo":
            return "/usr/bin/sudo"
        return None

    monkeypatch.setattr(claude_env.shutil, "which", _which)
    monkeypatch.setattr(claude_env, "_resolve_home", lambda _u: "/home/autoclaude")
    wrapped = wrap_for_user(["claude"])
    assert wrapped[:5] == ["sudo", "-E", "-u", "autoclaude", "--"]
    assert wrapped[5:] == ["env", "HOME=/home/autoclaude", "claude"]


def test_wrap_for_user_raises_when_no_tooling(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(claude_env.shutil, "which", lambda _name: None)
    with pytest.raises(UserCreationError) as excinfo:
        wrap_for_user(["claude"])
    assert "issue" in str(excinfo.value).lower()


def test_summarize_runtime_unset_non_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = tmp_path / "home"
    home.mkdir()
    cwd = tmp_path / "repo"
    cwd.mkdir()
    monkeypatch.setattr(claude_env.os, "geteuid", lambda: 1000)
    monkeypatch.setattr(claude_env.pwd, "getpwuid", lambda _u: type("E", (), {"pw_name": "alice"})())
    monkeypatch.setattr(claude_env, "_user_exists", lambda _u: False)

    snap = claude_env.summarize_runtime(home=home, cwd=cwd)
    assert snap["effective_default_mode"] == "<unset>"
    assert snap["user_settings_default_mode"] == "<unset>"
    assert snap["project_settings_default_mode"] == "<unset>"
    assert snap["claude_permission_mode"] == "bypassPermissions"
    assert snap["claude_runs_as"] == "alice"
    assert snap["autoclaude_user_required"] is False
    assert snap["autoclaude_user_exists"] is False


def test_summarize_runtime_auto_mode_user_settings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = tmp_path / "home"
    cwd = tmp_path / "repo"
    cwd.mkdir(parents=True)
    _write_settings(home / ".claude" / "settings.json", {"permissions": {"defaultMode": "auto"}})
    monkeypatch.setattr(claude_env.os, "geteuid", lambda: 0)
    monkeypatch.setattr(claude_env.pwd, "getpwuid", lambda _u: type("E", (), {"pw_name": "root"})())
    monkeypatch.setattr(claude_env, "_user_exists", lambda _u: True)

    snap = claude_env.summarize_runtime(home=home, cwd=cwd)
    assert snap["user_settings_default_mode"] == "auto"
    assert snap["project_settings_default_mode"] == "<unset>"
    assert snap["effective_default_mode"] == "auto"
    assert snap["claude_permission_mode"] == "<unset>"
    # Root + auto mode -> claude runs as root, no autoclaude wrapper.
    assert snap["claude_runs_as"] == "root"
    assert snap["autoclaude_user_required"] is False


def test_summarize_runtime_root_bypass_with_user_provisioned(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    cwd = tmp_path / "repo"
    cwd.mkdir()
    monkeypatch.setattr(claude_env.os, "geteuid", lambda: 0)
    monkeypatch.setattr(claude_env.pwd, "getpwuid", lambda _u: type("E", (), {"pw_name": "root"})())
    monkeypatch.setattr(claude_env, "_user_exists", lambda u: u == claude_env.AUTOCLAUDE_USER)

    snap = claude_env.summarize_runtime(home=home, cwd=cwd)
    assert snap["claude_permission_mode"] == "bypassPermissions"
    assert snap["claude_runs_as"] == "autoclaude"
    assert snap["autoclaude_user_required"] is True
    assert snap["autoclaude_user_exists"] is True


def test_summarize_runtime_root_bypass_with_user_missing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the autoclaude user is required but not yet on the system, report the actual current user."""
    home = tmp_path / "home"
    home.mkdir()
    cwd = tmp_path / "repo"
    cwd.mkdir()
    monkeypatch.setattr(claude_env.os, "geteuid", lambda: 0)
    monkeypatch.setattr(claude_env.pwd, "getpwuid", lambda _u: type("E", (), {"pw_name": "root"})())
    monkeypatch.setattr(claude_env, "_user_exists", lambda _u: False)

    snap = claude_env.summarize_runtime(home=home, cwd=cwd)
    assert snap["claude_permission_mode"] == "bypassPermissions"
    assert snap["claude_runs_as"] == "root"  # not "autoclaude" — it doesn't exist yet
    assert snap["autoclaude_user_required"] is True
    assert snap["autoclaude_user_exists"] is False


def test_summarize_runtime_project_overrides_user(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = tmp_path / "home"
    cwd = tmp_path / "repo"
    cwd.mkdir(parents=True)
    _write_settings(home / ".claude" / "settings.json", {"permissions": {"defaultMode": "auto"}})
    _write_settings(cwd / ".claude" / "settings.json", {"permissions": {"defaultMode": "plan"}})
    monkeypatch.setattr(claude_env.os, "geteuid", lambda: 1000)
    monkeypatch.setattr(claude_env.pwd, "getpwuid", lambda _u: type("E", (), {"pw_name": "alice"})())
    monkeypatch.setattr(claude_env, "_user_exists", lambda _u: False)

    snap = claude_env.summarize_runtime(home=home, cwd=cwd)
    assert snap["user_settings_default_mode"] == "auto"
    assert snap["project_settings_default_mode"] == "plan"
    assert snap["effective_default_mode"] == "plan"
    assert snap["claude_permission_mode"] == "bypassPermissions"
    assert snap["autoclaude_user_required"] is False  # not root
    assert snap["claude_runs_as"] == "alice"


def test_grant_path_traversal_walks_ancestors(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    leaf = tmp_path / "a" / "b" / "c" / "claude"
    leaf.parent.mkdir(parents=True)
    leaf.write_text("#!/bin/sh\n", encoding="utf-8")
    invoked: list[list[str]] = []
    monkeypatch.setattr(claude_env, "_run_cmd", lambda argv, **_kw: invoked.append(argv) or True)
    # Force every ancestor to look "not world-executable" so we cover the full walk.
    monkeypatch.setattr(claude_env, "_has_other_execute", lambda _p: False)

    claude_env._grant_path_traversal(leaf)  # noqa: SLF001

    chgrp_targets = [c[-1] for c in invoked if c[0] == "chgrp"]
    chmod_targets = [c[-1] for c in invoked if c[0] == "chmod"]
    # Each ancestor between tmp_path's drive root and the leaf gets chgrp + chmod.
    expected_ancestors = [str(tmp_path / "a" / "b" / "c"), str(tmp_path / "a" / "b"), str(tmp_path / "a"), str(tmp_path)]
    for ancestor in expected_ancestors:
        assert ancestor in chgrp_targets
        assert ancestor in chmod_targets
    # Leaf itself was chgrp'd + chmod'd as well.
    assert str(leaf.absolute()) in chgrp_targets


def test_grant_path_traversal_skips_world_executable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    leaf = tmp_path / "claude"
    leaf.write_text("#!/bin/sh\n", encoding="utf-8")
    invoked: list[list[str]] = []
    monkeypatch.setattr(claude_env, "_run_cmd", lambda argv, **_kw: invoked.append(argv) or True)
    monkeypatch.setattr(claude_env, "_has_other_execute", lambda _p: True)

    claude_env._grant_path_traversal(leaf)  # noqa: SLF001

    # Every ancestor and the leaf itself are already world-executable -> no chgrp/chmod issued.
    assert invoked == []


def test_grant_path_traversal_is_cached(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    leaf = tmp_path / "deep" / "claude"
    leaf.parent.mkdir()
    leaf.write_text("x", encoding="utf-8")
    invoked: list[list[str]] = []
    monkeypatch.setattr(claude_env, "_run_cmd", lambda argv, **_kw: invoked.append(argv) or True)
    monkeypatch.setattr(claude_env, "_has_other_execute", lambda _p: False)

    claude_env._grant_path_traversal(leaf)  # noqa: SLF001
    first_calls = len(invoked)
    claude_env._grant_path_traversal(leaf)  # noqa: SLF001
    assert len(invoked) == first_calls


def test_share_claude_binary_walks_symlink_and_target(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    bin_dir = tmp_path / "root" / ".local" / "bin"
    bin_dir.mkdir(parents=True)
    target = tmp_path / "root" / ".local" / "share" / "claude" / "versions" / "2.1.123"
    target.parent.mkdir(parents=True)
    target.write_text("#!/bin/sh\n", encoding="utf-8")
    symlink = bin_dir / "claude"
    symlink.symlink_to(target)

    monkeypatch.setattr(claude_env.shutil, "which", lambda _name: str(symlink))
    granted: list[str] = []

    def _fake_grant(path: Path, **_kw: object) -> None:
        granted.append(str(path))

    monkeypatch.setattr(claude_env, "_grant_path_traversal", _fake_grant)
    claude_env.share_claude_binary()

    # Both the symlink path and its resolved target must be walked.
    assert str(symlink) in granted
    assert str(target) in granted


def test_share_claude_binary_skips_when_no_binary_on_path(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(claude_env.shutil, "which", lambda _name: None)
    called: list[str] = []
    monkeypatch.setattr(claude_env, "_grant_path_traversal", lambda *_a, **_kw: called.append("called"))
    claude_env.share_claude_binary()
    assert called == []


def test_share_claude_binary_is_cached(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    binary = tmp_path / "claude"
    binary.write_text("x", encoding="utf-8")
    monkeypatch.setattr(claude_env.shutil, "which", lambda _name: str(binary))
    invocations: list[str] = []
    monkeypatch.setattr(claude_env, "_grant_path_traversal", lambda path, **_kw: invocations.append(str(path)))

    claude_env.share_claude_binary()
    first = len(invocations)
    claude_env.share_claude_binary()
    assert len(invocations) == first


def test_share_claude_config_grants_ancestor_traversal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    home = tmp_path / "root"
    home.mkdir()
    (home / ".claude").mkdir()
    monkeypatch.setattr(claude_env.pwd, "getpwnam", lambda _u: _FakePwEntry(tmp_path / "home" / "autoclaude"))
    monkeypatch.setattr(claude_env, "_run_cmd", lambda *_a, **_kw: True)
    granted: list[str] = []
    monkeypatch.setattr(claude_env, "_grant_path_traversal", lambda path, **_kw: granted.append(str(path)))

    share_claude_config(home=home)

    assert str(home / ".claude") in granted


def test_log_mode_once_dedupes(monkeypatch: pytest.MonkeyPatch) -> None:
    """Dedupe is asserted via the cache state since autoclaude's logger does not propagate to caplog."""
    emitted: list[str] = []
    monkeypatch.setattr(claude_env._log, "info", lambda fmt, *a, **_kw: emitted.append(fmt % a if a else fmt))  # noqa: SLF001
    claude_env.log_mode_once("hello")
    claude_env.log_mode_once("hello")
    claude_env.log_mode_once("world")
    assert emitted == ["hello", "world"]
