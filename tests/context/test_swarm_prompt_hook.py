import pytest

import hooks.context.swarm_heartbeat as swarm_heartbeat
from hooks import hook_manager


@pytest.fixture
def calls(monkeypatch):
    seen = []
    monkeypatch.setattr(swarm_heartbeat, "beat", lambda state: seen.append(("beat", state)))
    monkeypatch.setattr(swarm_heartbeat, "heard", lambda prompt: seen.append(("heard", prompt)))
    return seen


def test_a_prompt_beats_working_and_records_its_text(calls):
    hook_manager._swarm_heartbeat("working", "hold on")
    assert calls == [("beat", "working"), ("heard", "hold on")]


def test_a_tool_call_beats_without_recording_a_prompt(calls):
    hook_manager._swarm_heartbeat("idle")
    assert calls == [("beat", "idle")]


@pytest.mark.parametrize("payload, prompt", [({"prompt": "hold on"}, "hold on"), ({}, "")])
def test_user_prompt_submit_hands_its_prompt_to_the_heartbeat(monkeypatch, payload, prompt):
    seen = []
    monkeypatch.delenv("AGENTIHOOKS_SWARM", raising=False)
    monkeypatch.setattr(hook_manager, "_swarm_heartbeat", lambda *args: seen.append(args))
    hook_manager.on_user_prompt_submit({"session_id": "s1", "cwd": "/", **payload})
    assert seen[0] == ("working", prompt)
