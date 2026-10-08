import json
from types import SimpleNamespace

import ledger_core as core
import ledger_server
import new_ledger
import pytest

from tests.swarm_ledger import legacy_page  # noqa: E402

pytestmark = pytest.mark.xdist_group("fakeredis")

SLUG = "note-comments-2026-01-01"


def make_note(text="Keep replies under this note."):
    doc = new_ledger.build_doc(
        {"title": "Notes", "overview": "o", "sources": [], "phases": [{"title": "one", "description": "d"}]}
    )
    doc["notes"] = [{"id": "n1", "by": "operator", "at": 1, "text": text}]
    core.paths(SLUG)[0].write_text(legacy_page.render(doc, SLUG, 8765))
    core.paths(SLUG)[1].unlink(missing_ok=True)
    return core.sync(SLUG)[0]


def note_comment(text="Can you answer here?", by=None):
    op = {"op": "add", "thread": "notes/n1/comments", "id": "c1", "text": text}
    if by:
        op["by"] = by
    return op


def test_note_comment_is_stored_and_survives_loading_old_notes():
    make_note()
    core.check_op(note_comment())
    state, rejected = core.sync(SLUG, ops=[note_comment()])
    assert rejected == []
    assert state["notes"][0]["comments"][0]["text"] == "Can you answer here?"
    assert core.sync(SLUG)[0]["notes"][0]["comments"] == state["notes"][0]["comments"]


def test_invalid_note_comment_entries_are_rejected():
    doc = make_note()
    doc["notes"][0]["comments"] = [{"id": "c1", "text": 42}]
    with pytest.raises(ValueError, match="notes/n1/comments"):
        core.validate(doc)


@pytest.fixture
def inbox(monkeypatch):
    import fakeredis

    from scripts.inbox.store import InboxStore
    from scripts.swarm.store import RedisStore, SwarmConfig

    box = InboxStore(fakeredis.FakeRedis(server=fakeredis.FakeServer(), decode_responses=True))
    RedisStore(box.redis).create(SwarmConfig(SLUG, "/repo", 1, 0))
    monkeypatch.setattr("scripts.inbox.store.connect", lambda environ=None: box)
    return box


def test_operator_note_comment_reaches_master_with_original_note(inbox):
    make_note()
    state, rejected = core.sync(SLUG, ops=[note_comment()])
    assert rejected == []
    ledger_server.relay_to_inbox(SLUG, state)
    [message] = inbox.pending_items(f"master@{SLUG}")
    assert "Can you answer here?" in message.text
    assert "Keep replies under this note." in message.text


def test_addressed_note_comment_reaches_addressee(inbox):
    from scripts.swarm.store import AgentRecord, RedisStore

    store = RedisStore(inbox.redis)
    store.put_agent(SLUG, AgentRecord("engineer", "eng", "t1", seat=f"eng-1@{SLUG}"))
    make_note("@engineer Please answer under this note.")
    state, _ = core.sync(SLUG, ops=[note_comment()])
    ledger_server.relay_to_inbox(SLUG, state)
    [message] = inbox.pending_items(f"eng-1@{SLUG}")
    assert "Please answer under this note." in message.text
    assert inbox.pending_items(f"master@{SLUG}") == []


def test_agent_comment_command_stores_reply_under_note(monkeypatch, capsys):
    import ledger

    make_note()
    monkeypatch.setattr(ledger, "call", lambda slug, ops: {**core.sync(slug, ops=ops)[0]})
    ledger.cmd_comment(SimpleNamespace(slug=SLUG, item="notes/n1", text="The reply belongs here.", name="master"))
    assert json.loads(capsys.readouterr().out) == {"posted": True}
    assert core.sync(SLUG)[0]["notes"][0]["comments"][0]["text"] == "The reply belongs here."


def test_note_comment_owner_matches_the_addressed_note(inbox):
    import ledger_gate

    make_note("@engineer Answer here.")
    state, _ = core.sync(SLUG, ops=[note_comment()])
    event = state["_meta"]["events"][-1]
    members = {"master": {"role": "orchestrator"}, "engineer": {"role": "member"}}
    assert ledger_gate.owner(event, members) == "engineer"


def test_server_accepts_note_comment_and_agent_command_reply(inbox, monkeypatch, capsys):
    import threading
    import urllib.request
    from http.server import ThreadingHTTPServer

    import ledger

    make_note()
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), ledger_server.Handler)
    base = f"http://127.0.0.1:{httpd.server_port}"
    monkeypatch.setattr(ledger_server, "ALLOWED_HOSTS", {f"127.0.0.1:{httpd.server_port}"})
    monkeypatch.setattr(ledger, "BASE", base)
    threading.Thread(target=httpd.serve_forever, args=(0.01,), daemon=True).start()
    try:
        token = legacy_page.stored_token(core.paths(SLUG)[0])
        request = urllib.request.Request(
            f"{base}/api/{SLUG}",
            data=json.dumps({"ops": [note_comment()]}).encode(),
            method="PUT",
            headers={"Content-Type": "application/json", "X-Ledger-Token": token},
        )
        with urllib.request.urlopen(request) as response:
            assert json.load(response)["rejected"] == []
        [message] = inbox.pending_items(f"master@{SLUG}")
        assert "Keep replies under this note." in message.text
        ledger.cmd_comment(SimpleNamespace(slug=SLUG, item="notes/n1", text="The reply belongs here.", name="master"))
        assert json.loads(capsys.readouterr().out) == {"posted": True}
        state, _ = core.sync(SLUG)
        assert [c["text"] for c in state["notes"][0]["comments"]] == ["Can you answer here?", "The reply belongs here."]
    finally:
        httpd.shutdown()
        httpd.server_close()
