from pathlib import Path
from typing import Any

import pytest

from scripts.swarm_ledger import ledger_gate, ledger_hook
from tests.swarm_ledger import legacy_page

NAME = "engineer@a1-1"


@pytest.fixture
def folder(tmp_path, monkeypatch):
    monkeypatch.setenv("LEDGER_DIR", str(tmp_path))
    monkeypatch.setattr(ledger_hook, "LEDGER_DIR", tmp_path)
    monkeypatch.setattr(ledger_hook, "SESSIONS", tmp_path / ".sessions")
    return tmp_path


def refuse(*args, **kwargs):
    raise OSError("no server")


def test_a_folder_with_stored_ledgers_starts_the_server_and_an_empty_one_does_not(folder, monkeypatch):
    started = []
    monkeypatch.delenv("LEDGER_AUTOSTART", raising=False)
    monkeypatch.setattr(ledger_hook.socket, "create_connection", refuse)
    monkeypatch.setattr(ledger_hook.subprocess, "Popen", lambda argv, **kwargs: started.append(argv[-1]))
    ledger_hook.serve_ledgers()
    assert started == []
    legacy_page.store(folder, "demo", {"title": "Demo"})
    ledger_hook.serve_ledgers()
    assert started == ["--ensure"]


def test_dispatch_hands_only_the_gate_parts_of_the_bound_ledger_to_the_handler(folder, monkeypatch):
    members = {NAME: {"role": "member"}}
    doc = {"title": "Demo", "overview": "unread", "tasks": [{"id": "t1"}], "_meta": {"members": members}}
    legacy_page.store(folder, "demo", doc)
    (folder / ".sessions").mkdir()
    ledger_hook.write_session(folder / ".sessions" / "s1.json", ledger_hook.new_session("demo", NAME, "member"))
    seen = []
    monkeypatch.setitem(ledger_hook.HANDLERS, "PostToolUse", lambda payload, session, state, sfile: seen.append(state))
    ledger_hook.dispatch({"session_id": "s1", "hook_event_name": "PostToolUse", "tool_name": "Read"})
    assert [("tasks" in state, state["_meta"]["members"], "overview" in state) for state in seen] == [
        (False, members, False)
    ]


@pytest.mark.parametrize("members", [{}, {"another-worker": {"role": "member"}}])
def test_dispatch_does_not_run_the_handler_for_a_session_outside_the_ledger_membership(folder, monkeypatch, members):
    legacy_page.store(folder, "demo", {"_meta": {"members": members}})
    ledger_hook.write_session(folder / ".sessions" / "s1.json", ledger_hook.new_session("demo", NAME, "member"))
    seen = []
    monkeypatch.setitem(ledger_hook.HANDLERS, "PostToolUse", lambda *args: seen.append(args))
    ledger_hook.dispatch({"session_id": "s1", "hook_event_name": "PostToolUse", "tool_name": "Read"})
    assert seen == []


@pytest.mark.parametrize("event", ["PostToolUse", "Stop"])
@pytest.mark.parametrize("phase", [{"done": True}, {"out_of_scope": True}])
def test_dispatch_does_not_run_the_handler_for_a_closed_ledger(folder, monkeypatch, event, phase):
    doc = {"phases": [phase], "_meta": {"members": {NAME: {"role": "member"}}}}
    legacy_page.store(folder, "demo", doc)
    ledger_hook.write_session(folder / ".sessions" / "s1.json", ledger_hook.new_session("demo", NAME, "member"))
    seen = []
    monkeypatch.setitem(ledger_hook.HANDLERS, event, lambda *args: seen.append(args))
    ledger_hook.dispatch({"session_id": "s1", "hook_event_name": event, "tool_name": "Read"})
    assert seen == []


@pytest.mark.parametrize("event", ["PostToolUse", "Stop"])
def test_dispatch_does_not_run_the_handler_for_an_unreadable_ledger(folder, monkeypatch, event):
    ledger_hook.write_session(folder / ".sessions" / "s1.json", ledger_hook.new_session("demo", NAME, "member"))
    seen = []
    monkeypatch.setitem(ledger_hook.HANDLERS, event, lambda *args: seen.append(args))
    ledger_hook.dispatch({"session_id": "s1", "hook_event_name": event, "tool_name": "Read"})
    assert seen == []


@pytest.mark.parametrize("configured", [None, "~/development-ledger"])
def test_dispatch_reads_the_current_home_folder_after_an_earlier_event(folder, monkeypatch, configured):
    from unittest.mock import Mock

    from scripts.swarm_ledger import storage

    ledger_hook.dispatch({"session_id": "earlier", "hook_event_name": "PostToolUse", "tool_name": "Read"})
    home = folder / "home"
    current = home / "development-ledger"
    (current / ".sessions").mkdir(parents=True)
    ledger_hook.write_session(current / ".sessions" / "s1.json", ledger_hook.new_session("closed", NAME, "member"))
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(ledger_hook.Path, "home", classmethod(lambda cls: home))
    if configured is None:
        monkeypatch.delenv("LEDGER_DIR")
    else:
        monkeypatch.setenv("LEDGER_DIR", configured)
    read = Mock(wraps=storage.read_ledger)
    monkeypatch.setattr(storage, "read_ledger", read)

    ledger_hook.dispatch({"session_id": "s1", "hook_event_name": "PostToolUse", "tool_name": "Read"})

    assert read.call_count == 1
    assert read.call_args.args[:2] == (current, "closed")


BOSS = "boss@a1-0"
PEER = "peer@a1-2"


def operator(rev: int, target: str, kind: str = "comment", **extra: Any) -> dict[str, Any]:
    return {"rev": rev, "by": "operator", "kind": kind, "target": target, **extra}


def routed_ledger() -> dict[str, Any]:
    members = {
        NAME: {"role": "member", "handled_rev": 1, "claims": ["tasks/t4"]},
        BOSS: {"role": "orchestrator", "handled_rev": 0, "claims": []},
        PEER: {"role": "member", "handled_rev": 0, "claims": ["tasks/t3"]},
    }
    events = [
        operator(1, "tasks/t1"),
        operator(2, "tasks/t1"),
        operator(3, "tasks/t2"),
        operator(4, "tasks/t3"),
        operator(5, "tasks/t4"),
        operator(6, "chat", text="hello"),
        operator(7, "chat", text=f"@{NAME} look"),
        operator(8, "chat", kind="sync requested"),
        {"rev": 9, "by": PEER, "kind": "comment", "target": "tasks/t5"},
        operator(10, "tasks/t6"),
        operator(11, "tasks/t5", kind="chat cleared"),
    ]
    tasks = [
        {"id": "t1", "claimed_by": NAME, "description": "first"},
        {"id": "t2", "claimed_by": BOSS},
        {"id": "t3"},
        {"id": "t4", "claimed_by": "gone@a1-9"},
        {"id": "t5", "claimed_by": NAME},
    ]
    return {
        "tasks": tasks,
        "phases": [{"id": "p1", "done": True}, {"id": "p2"}],
        "followups": [{"id": "f1", "done": True}],
        "_meta": {"members": members, "events": events},
    }


def bind(folder: Path, doc: dict[str, Any]) -> None:
    legacy_page.store(folder, "demo", doc)
    ledger_hook.write_session(folder / ".sessions" / "s1.json", ledger_hook.new_session("demo", NAME, "member"))


def gate_decisions(folder: Path, monkeypatch: pytest.MonkeyPatch, doc: dict[str, Any], event: str) -> list[Any]:
    bind(folder, doc)
    seen = []

    def decide(payload: dict[str, Any], session: dict[str, Any], state: dict[str, Any], sfile: Path) -> None:
        owed = ledger_gate.unhandled_for(state["_meta"], session["name"], state.get("tasks", []))
        seen.append(([event["rev"] for event in owed], ledger_gate.policy(state)))

    monkeypatch.setitem(ledger_hook.HANDLERS, event, decide)
    ledger_hook.dispatch({"session_id": "s1", "hook_event_name": event, "tool_name": "Read"})
    return seen


def ledger_gate_defaults() -> dict[str, int]:
    return dict(ledger_gate.DEFAULT_POLICY)


@pytest.mark.feature("ledger-hook-bounded-read")
@pytest.mark.parametrize("event", ["PostToolUse", "UserPromptSubmit", "Stop"])
def test_dispatch_routes_owed_operator_events_to_the_task_claimant_claims_holder_or_orchestrator(
    folder: Path, monkeypatch: pytest.MonkeyPatch, event: str
) -> None:
    assert gate_decisions(folder, monkeypatch, routed_ledger(), event) == [([2, 5, 7, 8], ledger_gate_defaults())]


@pytest.mark.feature("ledger-hook-bounded-read")
def test_dispatch_hands_the_ledger_policy_and_reaches_the_orchestrator_for_a_task_the_ledger_lacks(
    folder: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    doc = {
        "policy": {"stop_after_calls": 3, "nudge_after_calls": "often"},
        "_meta": {"members": {NAME: {"role": "orchestrator", "handled_rev": 0}}, "events": [operator(2, "tasks/t9")]},
    }
    assert gate_decisions(folder, monkeypatch, doc, "Stop") == [
        ([2], {**ledger_gate_defaults(), "stop_after_calls": 3})
    ]


@pytest.mark.feature("ledger-hook-bounded-read")
@pytest.mark.parametrize(
    ("phases", "followups", "runs"),
    [
        ([{"id": "p1", "done": True}], [{"id": "f1"}], True),
        ([{"id": "p1", "done": True}], [{"id": "f1", "done": False}], True),
        ([{"id": "p1", "done": True}], [{"id": "f1", "done": True}], False),
        ([{"id": "p1", "out_of_scope": True}], [{"id": "f1", "out_of_scope": True}], False),
        ([{"done": True}, {"id": "p2", "title": "open"}], [], True),
        ([{"done": True}, {"id": "p2", "out_of_scope": True, "done": False}], [], False),
        ([], [{"id": "f1", "done": True}], True),
    ],
)
def test_dispatch_skips_only_a_ledger_whose_phases_and_follow_ups_are_all_done_or_out_of_scope(
    folder: Path, monkeypatch: pytest.MonkeyPatch, phases: list[Any], followups: list[Any], runs: bool
) -> None:
    doc = {"phases": phases, "followups": followups, "_meta": {"members": {NAME: {"role": "member"}}}}
    assert gate_decisions(folder, monkeypatch, doc, "Stop") == ([([], ledger_gate_defaults())] if runs else [])


@pytest.mark.feature("ledger-hook-bounded-read")
def test_dispatch_for_a_bound_session_never_reads_the_whole_tasks_phases_or_followups(
    folder: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from scripts.swarm_ledger.repository import sqlite

    bind(folder, routed_ledger())
    asked = []
    partial = sqlite.read_partial

    def spy(connection: Any, slug: str, keys: tuple[Any, ...]) -> dict[str, Any]:
        asked.extend(keys)
        return partial(connection, slug, keys)

    def whole(*args: Any) -> None:
        raise AssertionError("the ledger hook read every row of the ledger")

    monkeypatch.setattr(sqlite, "read_partial", spy)
    monkeypatch.setattr(sqlite, "read_rows", whole)
    seen = []
    monkeypatch.setitem(ledger_hook.HANDLERS, "PostToolUse", lambda payload, session, state, sfile: seen.append(state))
    ledger_hook.dispatch({"session_id": "s1", "hook_event_name": "PostToolUse", "tool_name": "Read"})

    assert [key for key in asked if key in (["tasks"], ["phases"], ["followups"])] == []
    assert sorted(key[1][1] for key in asked if key[0] == "tasks") == ["t1", "t2", "t3", "t4", "t6"]
    assert [(sorted(state), [task["id"] for task in state["tasks"]]) for state in seen] == [
        (["_meta", "tasks"], ["t1", "t2", "t3", "t4"])
    ]
