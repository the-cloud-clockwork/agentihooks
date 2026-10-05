import json

import pytest

from scripts import init_agent, install
from tests.test_install import TestInstallGlobalHonoursTarget as InstallHelper


@pytest.mark.parametrize("target", ["claude", "codex"])
@pytest.mark.parametrize("enabled", [True, False])
def test_installed_profile_sets_langfuse_switch(tmp_path, monkeypatch, target, enabled):
    helper = InstallHelper()
    original = helper._tiny_profile

    def profiles(root):
        directory = original(root)
        (directory / "tiny" / "profile.yml").write_text(f"otel:\n  langfuse:\n    enabled: {str(enabled).lower()}\n")
        return directory

    monkeypatch.setattr(helper, "_tiny_profile", profiles)
    monkeypatch.setattr(install, "_seed_user_env_file", lambda: None)
    home = helper._run(tmp_path, monkeypatch, target)
    expected = "1" if enabled else "0"
    if target == "claude":
        settings = json.loads((home / ".claude" / "settings.json").read_text())
        assert settings["env"]["AGENTIHOOKS_LANGFUSE_ENABLED"] == expected
    else:
        wrapper = (home / ".codex" / "agentihooks-hook.sh").read_text()
        assert f"AGENTIHOOKS_LANGFUSE_ENABLED:={expected}" in wrapper


@pytest.mark.parametrize("agent", ["claude", "codex"])
@pytest.mark.parametrize("enabled", [True, False])
def test_launcher_uses_installed_profile_outside_swarm(tmp_path, monkeypatch, agent, enabled):
    profile = tmp_path / "profile"
    profile.mkdir()
    (profile / "profile.yml").write_text(f"otel:\n  langfuse:\n    enabled: {str(enabled).lower()}\n")
    monkeypatch.setattr(install, "_load_state", lambda: {"targets": {"global": {agent: {"profile": "tiny"}}}})
    monkeypatch.setattr(install, "_resolve_profile_dir", lambda name: profile)
    launcher, _ = init_agent._write_launcher(
        tmp_path, "manual", "", [], {"XDG_RUNTIME_DIR": str(tmp_path)}, init_agent.AgentSpec(agent=agent)
    )
    text = launcher.read_text()
    assert f"export AGENTIHOOKS_LANGFUSE_ENABLED={int(enabled)}\n" in text
    assert "export AGENTIHOOKS_SWARM=" not in text


def test_settings_profile_can_disable_all_telemetry(tmp_path):
    from scripts.profile_telemetry import langfuse_env

    profile = tmp_path / "anton"
    overlay = tmp_path / "off"
    profile.mkdir()
    overlay.mkdir()
    (profile / "profile.yml").write_text("otel:\n  enabled: true\n  langfuse:\n    enabled: true\n")
    (overlay / "profile.yml").write_text("otel:\n  enabled: false\n")
    assert langfuse_env([("anton", profile)], overlay) == {"AGENTIHOOKS_LANGFUSE_ENABLED": "0"}
