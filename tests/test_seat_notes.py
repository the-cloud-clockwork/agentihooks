"""The seat notes reminder on every user prompt, per harness target."""

import json
from unittest.mock import patch

import pytest

SEAT_LINE = "SEAT NOTES: run `agentihooks swarm demo-swarm learned`"
TARGETS = ("claude", "codex", "copilot")


def _prompt_output(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], target: str) -> str:
    import hooks.hook_manager as hm
    from hooks.targets import emitter

    if target == "claude":
        monkeypatch.delenv("AGENTIHOOKS_TARGET", raising=False)
    else:
        monkeypatch.setenv("AGENTIHOOKS_TARGET", target)
    emitter.drain()
    capsys.readouterr()
    with patch("hooks._redis.get_redis", return_value=None):
        hm.on_user_prompt_submit(
            {"hook_event_name": "UserPromptSubmit", "session_id": f"seat-{target}", "prompt": "go", "cwd": ""}
        )
        emitter.flush("UserPromptSubmit")
    return capsys.readouterr().out


def _seat(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGENTIHOOKS_SWARM", "demo-swarm")
    monkeypatch.setenv("AGENTIHOOKS_AGENT_NAME", "engineer@323133-4958")


def _unseat(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("AGENTIHOOKS_SWARM", raising=False)
    monkeypatch.delenv("AGENTIHOOKS_AGENT_NAME", raising=False)


@pytest.mark.parametrize("target", TARGETS)
def test_a_seated_session_gets_the_seat_notes_line_on_a_user_prompt(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], target: str
) -> None:
    _seat(monkeypatch)

    out = _prompt_output(monkeypatch, capsys, target)

    assert SEAT_LINE in out
    if target != "claude":
        envelope = json.loads(out.strip().splitlines()[-1])
        assert SEAT_LINE in json.dumps(envelope)


@pytest.mark.parametrize("target", TARGETS)
def test_an_unseated_session_gets_no_seat_notes_line(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], target: str
) -> None:
    _unseat(monkeypatch)

    assert "SEAT NOTES" not in _prompt_output(monkeypatch, capsys, target)


def test_a_swarm_without_an_agent_name_is_not_a_seat() -> None:
    from hooks.context.seat_notes import reminder

    assert reminder({"AGENTIHOOKS_SWARM": "demo-swarm", "AGENTIHOOKS_AGENT_NAME": "someone"}) == ""
    assert reminder({"AGENTIHOOKS_AGENT_NAME": "engineer@323133-4958"}) == ""


def test_the_line_names_the_learned_command_for_the_seats_swarm() -> None:
    from hooks.context.seat_notes import reminder

    line = reminder({"AGENTIHOOKS_SWARM": "demo-swarm", "AGENTIHOOKS_AGENT_NAME": "engineer@323133-1"})

    assert line == (
        "SEAT NOTES: run `agentihooks swarm demo-swarm learned` before any tool call for context "
        "your seat may already hold."
    )
