"""Windows-specific seams of the installer: no fcntl, native codex skill layers."""

import argparse
import json
import sys
from pathlib import Path

import install


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
