import pytest

from scripts.swarm_ledger import ledger_hook
from tests.swarm_ledger import legacy_page

NAME = "engineer@a1-1"


@pytest.fixture
def folder(tmp_path, monkeypatch):
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
