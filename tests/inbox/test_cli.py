import json

import pytest

from scripts.inbox import cli
from scripts.inbox.store import InboxStore

pytestmark = pytest.mark.xdist_group("fakeredis")


@pytest.fixture
def store(monkeypatch):
    import fakeredis

    store = InboxStore(fakeredis.FakeRedis(decode_responses=True))
    monkeypatch.setattr(cli, "connect", lambda: store)
    monkeypatch.setattr(cli, "registered_name", lambda: "", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    monkeypatch.setenv("AGENTIHOOKS_AGENT_NAME", "alice")
    return store


def run(*argv):
    return cli.main(list(argv))


def test_send_then_inbox_shows_a_pending_item_from_the_session(store, monkeypatch, capsys):
    assert run("send", "bob", "review", "my", "branch") == 0
    sent = json.loads(capsys.readouterr().out)
    monkeypatch.setenv("AGENTIHOOKS_AGENT_NAME", "bob")
    assert run("inbox") == 0
    assert capsys.readouterr().out.split("\t")[:3] == [sent["id"], "pending", "alice"]
    assert store.get(sent["id"]).text == "review my branch"


@pytest.mark.parametrize(
    "argv",
    [
        ("send", "bob", "hi", "--from", "mallory"),
        ("send", "bob", "--as", "mallory", "hi"),
        ("send", "bob", "--sender=mallory", "hi"),
    ],
)
def test_a_forged_sender_argument_is_ignored(store, argv):
    assert run(*argv) == 0
    [item] = store.inbox("bob")
    assert item.sender == "alice"
    assert item.text == " ".join(argv[2:])


def test_a_sender_option_before_the_address_sends_nothing(store):
    with pytest.raises(SystemExit):
        run("send", "--from", "mallory", "bob", "hi")
    assert store.inbox("bob") == []


def test_the_session_id_is_the_sender_without_an_agent_name(store, monkeypatch):
    monkeypatch.delenv("AGENTIHOOKS_AGENT_NAME")
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "sess-1")
    run("send", "bob", "hi")
    assert store.inbox("bob")[0].sender == "sess-1"


def test_a_session_named_on_its_record_speaks_under_that_name(store, monkeypatch):
    monkeypatch.delenv("AGENTIHOOKS_AGENT_NAME")
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "sess-1")
    monkeypatch.setattr(cli, "registered_name", lambda: "sw-master-1")
    run("send", "bob", "hi")
    assert store.inbox("bob")[0].sender == "sw-master-1"


def test_a_session_without_identity_is_refused(store, monkeypatch, capsys):
    monkeypatch.delenv("AGENTIHOOKS_AGENT_NAME")
    assert run("send", "bob", "hi") == 1
    assert "identity" in capsys.readouterr().err
    assert store.inbox("bob") == []


def test_read_shows_the_item_and_marks_it_read(store, monkeypatch, capsys):
    item = store.send("alice", "bob", "hi")
    monkeypatch.setenv("AGENTIHOOKS_AGENT_NAME", "bob")
    assert run("read", item.id) == 0
    shown = json.loads(capsys.readouterr().out)
    assert (shown["text"], shown["state"]) == ("hi", "read")
    assert [h["state"] for h in shown["history"]] == ["pending", "read"]


def test_closing_without_a_reason_is_refused(store, monkeypatch, capsys):
    item = store.send("alice", "bob", "hi")
    monkeypatch.setenv("AGENTIHOOKS_AGENT_NAME", "bob")
    assert run("close", item.id) == 1
    assert "reason" in capsys.readouterr().err
    assert store.get(item.id).state == "pending"


def test_close_with_a_handoff_names_the_address(store, monkeypatch, capsys):
    item = store.send("alice", "bob", "hi")
    monkeypatch.setenv("AGENTIHOOKS_AGENT_NAME", "bob")
    assert run("close", item.id, "handoff", "carol") == 0
    assert json.loads(capsys.readouterr().out)["reason"] == "handed off to carol"
    assert [h["state"] for h in store.history(item.id)] == ["pending", "handed_off"]


def test_another_sessions_item_is_refused(store, capsys):
    item = store.send("bob", "carol", "hi")
    assert run("read", item.id) == 1
    assert "belongs to carol" in capsys.readouterr().err


def test_reply_answers_the_sender_and_closes_the_item(store, monkeypatch, capsys):
    item = store.send("bob", "alice", "is the branch ready")
    assert run("reply", item.id, "yes,", "pushed") == 0
    out = json.loads(capsys.readouterr().out)
    assert (out["to"], out["closed"]) == ("bob", item.id)
    [answer] = store.inbox("bob")
    assert (answer.sender, answer.text) == ("alice", "yes, pushed")
    assert store.get(item.id).state == "done"


def test_multi_agent_chat_rooms_are_retired_without_dangling_references():
    import subprocess
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    assert not (root / "profiles" / "package" / "skills" / "multi-agent-chat").exists()
    tracked = subprocess.run(
        ["git", "grep", "-l", "multi-agent-chat"], cwd=root, capture_output=True, text=True
    ).stdout.split()
    assert set(tracked) <= {"CHANGELOG.md", "profiles/_base/lifecycle.json", "tests/inbox/test_cli.py"}
