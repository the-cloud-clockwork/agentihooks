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
    monkeypatch.setattr(common, "inject_context", lambda message, *a, **k: captured.append((message, k)))
    return captured


SHOWN = (NOTE, {"also_log": False, "skip_compression": True, "kind": "refocus"})


def test_run_returns_each_context_and_the_block(monkeypatch):
    def fake(payload):
        print(_context("PostToolUse"))
        print("not json")
        print("null")
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
    assert SHOWN in injected


def test_user_prompt_shows_ledger_events_on_every_harness(harness, dispatched, injected):
    hm.on_user_prompt_submit({"hook_event_name": "UserPromptSubmit", "session_id": "s1", "prompt": "go", "cwd": "/p"})
    assert [p["hook_event_name"] for p in dispatched] == ["UserPromptSubmit"]
    assert SHOWN in injected


def test_session_start_reaches_the_ledger_hook_on_every_harness(harness, dispatched, injected, monkeypatch):
    monkeypatch.setattr("hooks.lifecycle.deps_kick.kick", lambda: None)
    monkeypatch.setattr("hooks.lifecycle.codex_refresh_kick.kick", lambda: None)
    hm.on_session_start({"hook_event_name": "SessionStart", "session_id": "s1", "cwd": "/p"})
    assert [p["hook_event_name"] for p in dispatched] == ["SessionStart"]


def test_a_held_stop_finishes_the_turn_but_schedules_no_refresh_or_close(harness, dispatched, monkeypatch):
    confirmed, scheduled = [], []
    monkeypatch.setattr(hm, "_confirm_inbox", confirmed.append)
    monkeypatch.setattr("hooks.lifecycle.refresh.on_stop", scheduled.append)
    monkeypatch.setattr("hooks.lifecycle.handoff_close.on_stop", scheduled.append)
    monkeypatch.setattr("hooks._async.fork_and_call", lambda *a, **k: None)
    monkeypatch.setattr(hm, "_request_trace_flush", lambda *a: None)
    with pytest.raises(hm.BlockAction, match="operator event"):
        hm.on_stop({"hook_event_name": "Stop", "session_id": "s1", "cwd": "/p"})
    assert [p["hook_event_name"] for p in dispatched] == ["Stop"]
    assert (confirmed, scheduled) == (["s1"], [])


def test_a_failing_ledger_hook_never_breaks_the_event(harness, monkeypatch, injected):
    logged = []

    def broken(payload):
        raise RuntimeError("ledger down")

    monkeypatch.setattr(ledger_hook, "dispatch", broken)
    monkeypatch.setattr(hm, "log", lambda message, data=None: logged.append((message, data)))
    hm._swarm_ledger({"hook_event_name": "UserPromptSubmit", "session_id": "s1"})
    assert logged == [("swarm ledger hook failed", {"error": "ledger down"})]
    assert injected == []


JOIN = "agentihooks ledger --slug demo --as eng-1 join"
JOINED = json.dumps({"joined": "eng-1", "slug": "demo", "role": "member"})
NEW = "agentihooks ledger new plan.md"
CREATED = json.dumps({"slug": "demo", "joined": "eng-1"})

RAW_TOOL_CALLS = {
    "claude": lambda command, out: {
        "hook_event_name": "PostToolUse",
        "session_id": "s1",
        "tool_name": "Bash",
        "tool_input": {"command": command},
        "tool_response": {"stdout": out, "stderr": ""},
    },
    "codex": lambda command, out: {
        "hook_event_name": "PostToolUse",
        "session_id": "s1",
        "tool_name": "Bash",
        "tool_input": {"command": command},
        "tool_response": out,
    },
    "copilot": lambda command, out: {
        "sessionId": "s1",
        "toolName": "bash",
        "toolArgs": json.dumps({"command": command}),
        "toolResult": {"resultType": "success", "textResultForLlm": out},
    },
}


@pytest.mark.parametrize(("command", "out"), [(JOIN, JOINED), (NEW, CREATED)], ids=["join", "new"])
def test_a_ledger_join_binds_the_session_from_each_harness_payload(harness, command, out, monkeypatch, tmp_path):
    from hooks.targets.normalizer import normalize_payload

    monkeypatch.setenv("AGENTIHOOKS_COPILOT_EVENT", "postToolUse")
    monkeypatch.setattr(ledger_hook, "SESSIONS", tmp_path)
    payload = normalize_payload(RAW_TOOL_CALLS[harness](command, out))
    ledger_hook.bind(payload, payload["session_id"])
    session = json.loads((tmp_path / "s1.json").read_text())
    assert (session["slug"], session["name"]) == ("demo", "eng-1")
