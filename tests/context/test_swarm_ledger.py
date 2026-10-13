"""The swarm ledger hook runs inside the shared hooks entry on every harness."""

import json

import pytest

import hooks.common as common
import hooks.hook_manager as hm
from hooks.context import swarm_ledger
from scripts.swarm_ledger import ledger_hook

pytestmark = pytest.mark.unit

NOTE = "SWARM LEDGER demo: 1 operator event(s) are unhandled"


def _context(event):
    return json.dumps({"hookSpecificOutput": {"hookEventName": event, "additionalContext": NOTE}})


@pytest.fixture(params=["claude", "codex", "copilot"])
def harness(request, monkeypatch):
    monkeypatch.setenv("AGENTIHOOKS_TARGET", request.param)
    return request.param


@pytest.fixture
def dispatched(monkeypatch):
    seen = []

    def fake(payload):
        seen.append(payload)
        event = payload["hook_event_name"]
        if event == "Stop":
            print(json.dumps({"decision": "block", "reason": NOTE}))
        elif event in ("PostToolUse", "UserPromptSubmit"):
            print(_context(event))

    monkeypatch.setattr(ledger_hook, "dispatch", fake)
    return seen


@pytest.fixture
def injected(monkeypatch):
    captured = []
    monkeypatch.setattr(common, "inject_context", lambda message, *a, **k: captured.append(message))
    return captured


def test_run_returns_each_context_and_the_block(monkeypatch):
    def fake(payload):
        print(_context("PostToolUse"))
        print("not json")
        print(json.dumps({"decision": "block", "reason": "stop held"}))

    monkeypatch.setattr(ledger_hook, "dispatch", fake)
    assert swarm_ledger.run({"hook_event_name": "PostToolUse"}) == ([NOTE], "stop held")


def test_run_passes_the_payload_and_returns_nothing_when_the_hook_is_silent(monkeypatch):
    seen = []
    monkeypatch.setattr(ledger_hook, "dispatch", seen.append)
    payload = {"hook_event_name": "UserPromptSubmit", "session_id": "s1"}
    assert swarm_ledger.run(payload) == ([], None)
    assert seen == [payload]


def test_run_skips_output_without_context_or_block(monkeypatch):
    monkeypatch.setattr(
        ledger_hook,
        "dispatch",
        lambda payload: print(json.dumps({"hookSpecificOutput": {"hookEventName": "PostToolUse"}, "decision": "x"})),
    )
    assert swarm_ledger.run({"hook_event_name": "PostToolUse"}) == ([], None)


def test_post_tool_use_shows_ledger_events_on_every_harness(harness, dispatched, injected):
    payload = {
        "hook_event_name": "PostToolUse",
        "session_id": "s1",
        "tool_name": "Bash",
        "tool_input": {"command": "ls"},
        "tool_output": "ok",
        "cwd": "/p",
    }
    hm.on_post_tool_use(payload)
    assert [p["hook_event_name"] for p in dispatched] == ["PostToolUse"]
    assert NOTE in injected


def test_user_prompt_shows_ledger_events_on_every_harness(harness, dispatched, injected):
    hm.on_user_prompt_submit({"hook_event_name": "UserPromptSubmit", "session_id": "s1", "prompt": "go", "cwd": "/p"})
    assert [p["hook_event_name"] for p in dispatched] == ["UserPromptSubmit"]
    assert NOTE in injected


def test_session_start_reaches_the_ledger_hook_on_every_harness(harness, dispatched, injected):
    hm.on_session_start({"hook_event_name": "SessionStart", "session_id": "s1", "cwd": "/p"})
    assert [p["hook_event_name"] for p in dispatched] == ["SessionStart"]


def test_stop_is_held_by_the_ledger_on_every_harness(harness, dispatched, monkeypatch):
    monkeypatch.setattr("hooks.lifecycle.refresh.on_stop", lambda payload: False)
    monkeypatch.setattr("hooks.lifecycle.handoff_close.on_stop", lambda payload: False)
    with pytest.raises(hm.BlockAction, match="operator event"):
        hm.on_stop({"hook_event_name": "Stop", "session_id": "s1", "cwd": "/p"})
    assert [p["hook_event_name"] for p in dispatched] == ["Stop"]


def test_a_failing_ledger_hook_never_breaks_the_event(harness, monkeypatch, injected):
    def broken(payload):
        raise RuntimeError("ledger down")

    monkeypatch.setattr(ledger_hook, "dispatch", broken)
    hm.on_user_prompt_submit({"hook_event_name": "UserPromptSubmit", "session_id": "s1", "prompt": "go", "cwd": "/p"})
