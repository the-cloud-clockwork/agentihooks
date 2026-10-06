from unittest.mock import patch

import pytest

import hooks.config
from hooks import hook_manager
from hooks.context import controls_toggle
from scripts.swarm import delivery

KEYWORDS = 'Learned note: the master quoted "enable voice" and "disable controls" because the operator asked once.'


@pytest.fixture(autouse=True)
def toggles(tmp_path, monkeypatch):
    from hooks.context import voice_output

    monkeypatch.setattr(hooks.config, "VOICE_ENABLED", True)
    monkeypatch.setattr(hooks.config, "CONTROLS_BYPASS_ENABLED", True)
    monkeypatch.setattr(voice_output, "_FLAG_DIR", tmp_path / "voice_flags")
    monkeypatch.setattr(voice_output, "_QUOTA_FLAG", tmp_path / "voice_flags" / "quota_exhausted")
    monkeypatch.setattr(hook_manager, "_swarm_heartbeat", lambda *args: None)
    monkeypatch.delenv("AGENTIHOOKS_DISABLE_BYPASS_LOOKUP", raising=False)
    for name in ("AGENTIHOOKS_SWARM", "AGENTIHOOKS_AGENT_NAME", "AGENTIHOOKS_SWARM_TASK"):
        monkeypatch.delenv(name, raising=False)
    with patch("hooks.context.voice_output.get_redis", return_value=None):
        yield voice_output


def submit(prompt, session="s1"):
    hook_manager.on_user_prompt_submit({"session_id": session, "cwd": "/", "prompt": prompt})


def test_a_swarm_opening_prompt_quoting_the_keywords_leaves_voice_and_controls_alone(toggles, monkeypatch):
    monkeypatch.setenv("AGENTIHOOKS_SWARM", "demo")
    monkeypatch.setenv("AGENTIHOOKS_AGENT_NAME", "master@a1-1")
    submit(f"You are master@a1-1, the master of swarm demo.\n{KEYWORDS}")
    assert not toggles.is_voice_enabled("s1")
    assert not controls_toggle.is_controls_disabled("s1")


def test_a_swarm_delivery_quoting_the_keywords_leaves_voice_and_controls_alone(toggles, monkeypatch):
    monkeypatch.setenv("AGENTIHOOKS_SWARM", "demo")
    monkeypatch.setenv("AGENTIHOOKS_AGENT_NAME", "eng-1@demo")
    submit("open the task")
    submit(f"{delivery.MARK} {KEYWORDS}")
    assert not toggles.is_voice_enabled("s1")
    assert not controls_toggle.is_controls_disabled("s1")


def test_the_operator_typing_the_keywords_still_toggles_both(toggles, monkeypatch):
    monkeypatch.setenv("AGENTIHOOKS_SWARM", "demo")
    monkeypatch.setenv("AGENTIHOOKS_AGENT_NAME", "master@a1-1")
    submit("You are master@a1-1, the master of swarm demo.")
    submit("enable voice and disable controls")
    assert toggles.is_voice_enabled("s1")
    assert controls_toggle.is_controls_disabled("s1")
    submit("disable voice and enable controls")
    assert not toggles.is_voice_enabled("s1")
    assert not controls_toggle.is_controls_disabled("s1")


def test_an_operator_session_outside_a_swarm_toggles_on_its_first_prompt(toggles):
    submit("enable voice, then disable controls")
    assert toggles.is_voice_enabled("s1")
    assert controls_toggle.is_controls_disabled("s1")
