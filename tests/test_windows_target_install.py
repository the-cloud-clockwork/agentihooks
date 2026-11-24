"""Windows-specific seams of the installer: no fcntl, native codex skill layers."""

import argparse
import json
import sys
from pathlib import Path

import pytest

from scripts import install


def _tiny_profile(tmp_path: Path) -> Path:
    profiles = tmp_path / "profiles"
    tiny = profiles / "tiny" / ".claude"
    tiny.mkdir(parents=True)
    (tiny / "CLAUDE.md").write_text("# tiny persona\n")
    (tiny / "settings.overrides.json").write_text("{}")
    base = profiles / "_base"
    base.mkdir()
    (base / "settings.base.json").write_text(json.dumps({"hooks": {}}))
    (base / "settings.base.copilot.json").write_text(json.dumps({"disableAllHooks": False}))
    (base / "config.base.toml").write_text('approval_policy = "on-request"\n')
    return profiles


def _run(profiles: Path, monkeypatch, target: str) -> Path:
    monkeypatch.setattr(install, "PROFILES_DIR", profiles, raising=False)
    monkeypatch.setattr(install, "_get_bundle_path", lambda: None)
    monkeypatch.setattr(install, "_install_cli_tool", lambda *a, **k: None, raising=False)
    monkeypatch.setattr(install, "_ensure_mcp_daemon", lambda *a, **k: None, raising=False)
    monkeypatch.setattr(install, "_resolve_hooks_python", lambda *a, **k: Path(sys.executable), raising=False)
    install.install_global(argparse.Namespace(profile="tiny", settings_profile="", install_target=target))
    return Path.home()


def test_codex_only_profile_skills_install_from_dot_codex_skills(tmp_path, monkeypatch):
    profiles = _tiny_profile(tmp_path)
    skill = profiles / "tiny" / ".codex" / "skills" / "my-skill"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("---\nname: my-skill\n---\nbody")
    _run(profiles, monkeypatch, "codex")
    from scripts.targets._common import agents_skills_home

    assert (agents_skills_home() / "my-skill").exists(), ".codex/skills layer was not installed"


def test_claude_target_ignores_dot_codex_skills(tmp_path, monkeypatch):
    profiles = _tiny_profile(tmp_path)
    skill = profiles / "tiny" / ".codex" / "skills" / "my-skill"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("---\nname: my-skill\n---\nbody")
    home = _run(profiles, monkeypatch, "claude")
    assert not (home / ".claude" / "skills" / "my-skill").exists()


def test_sync_lock_is_a_noop_where_fcntl_is_unavailable(monkeypatch):
    """Windows has no fcntl module; the lock degrades to no serialisation."""
    from scripts import platform_compat

    monkeypatch.setattr(platform_compat, "fcntl", None)
    entered = []
    with install._sync_lock():
        entered.append(True)
    assert entered == [True]


def test_codex_hooks_run_a_batch_wrapper_on_windows(monkeypatch):
    """cmd.exe opens a .sh hook in a new Git Bash window, so Windows hooks run a .cmd wrapper."""
    from scripts.targets import codex_target

    monkeypatch.delenv("CODEX_HOME", raising=False)
    monkeypatch.setattr(codex_target, "WINDOWS", True)
    home = codex_target.codex_home()
    home.mkdir(parents=True, exist_ok=True)
    codex_target.CodexAdapter().write_settings({})
    wrapper = home / "agentihooks-hook.cmd"
    commands = {
        hook["command"]
        for groups in json.loads((home / "hooks.json").read_text())["hooks"].values()
        for group in groups
        for hook in group["hooks"]
    }
    assert commands == {f'cmd.exe /d /c "{wrapper}"'}
    text = wrapper.read_text()
    assert text.startswith("@echo off\n")
    assert codex_target._wrapper_python(text) == text.splitlines()[-1].split('"')[1]


def test_windows_reinstall_replaces_bash_wrapper_hooks(monkeypatch):
    """A Windows install from before the batch wrapper left .sh hooks; reinstall must not keep them."""
    from scripts.targets import codex_target

    monkeypatch.delenv("CODEX_HOME", raising=False)
    monkeypatch.setattr(codex_target, "WINDOWS", True)
    home = codex_target.codex_home()
    home.mkdir(parents=True, exist_ok=True)
    legacy = {"hooks": [{"type": "command", "command": str(home / "agentihooks-hook.sh")}]}
    (home / "hooks.json").write_text(json.dumps({"hooks": {"PreToolUse": [legacy], "PostCompact": [legacy]}}))
    codex_target.CodexAdapter().write_settings({})
    doc = json.loads((home / "hooks.json").read_text())
    commands = [hook["command"] for groups in doc["hooks"].values() for group in groups for hook in group["hooks"]]
    assert not any(command.endswith("agentihooks-hook.sh") for command in commands)
    assert "PostCompact" not in doc["hooks"]


def test_windows_reinstall_replaces_quoted_wrapper_hooks(monkeypatch):
    """Codex on Windows never runs a quoted hook command; reinstall writes the cmd.exe form once per event."""
    from scripts.targets import codex_target

    monkeypatch.delenv("CODEX_HOME", raising=False)
    monkeypatch.setattr(codex_target, "WINDOWS", True)
    home = codex_target.codex_home()
    home.mkdir(parents=True, exist_ok=True)
    wrapper = home / "agentihooks-hook.cmd"
    quoted = {"hooks": [{"type": "command", "command": f'"{wrapper}"'}]}
    (home / "hooks.json").write_text(json.dumps({"hooks": {"PreToolUse": [quoted]}}))
    codex_target.CodexAdapter().write_settings({})
    groups = json.loads((home / "hooks.json").read_text())["hooks"]["PreToolUse"]
    assert [hook["command"] for group in groups for hook in group["hooks"]] == [f'cmd.exe /d /c "{wrapper}"']


@pytest.mark.parametrize("takes_package", [True, False])
def test_package_features_false_leaves_out_package_rules_and_fleet_preamble(tmp_path, monkeypatch, takes_package):
    """A single-user assistant profile opts out of agentihooks' own rules and the fleet identity preamble."""
    from scripts.targets.codex_target import codex_home

    monkeypatch.delenv("CODEX_HOME", raising=False)
    profiles = _tiny_profile(tmp_path)
    if not takes_package:
        (profiles / "tiny" / "profile.yml").write_text("name: tiny\npackage_features: false\n")
    _run(profiles, monkeypatch, "codex")
    text = (codex_home() / "AGENTS.md").read_text()
    assert ("rule: worktrees.md" in text) is takes_package
    assert ("operator's fleet" in text) is takes_package
    assert "You are **tiny**" in text
