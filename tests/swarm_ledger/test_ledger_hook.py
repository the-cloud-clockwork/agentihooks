import pytest

from scripts.swarm_ledger import ledger_hook
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
    assert [(state["tasks"], state["_meta"]["members"], "overview" in state) for state in seen] == [
        ([{"id": "t1"}], members, False)
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
