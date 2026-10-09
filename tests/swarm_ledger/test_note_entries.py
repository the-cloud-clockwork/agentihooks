from types import SimpleNamespace

from scripts.swarm_ledger import ledger_comments, ledger_core


def context():
    events = []
    return SimpleNamespace(
        at=42,
        meta={"members": {"eng": {"role": "member"}}},
        record=lambda *args, **fields: events.append((args, fields)),
        events=events,
    )


def test_a_note_entry_carries_an_empty_comments_thread():
    op = {"op": "add", "id": "n1", "thread": "notes"}
    assert ledger_core.new_entry(op, "operator", 42, "Later") == {
        "id": "n1",
        "by": "operator",
        "at": 42,
        "text": "Later",
        "comments": [],
    }


def test_an_entry_on_any_other_thread_has_no_comments_thread():
    op = {"op": "add", "id": "m1", "thread": "chat"}
    assert ledger_core.new_entry(op, "eng", 7, "hi") == {"id": "m1", "by": "eng", "at": 7, "text": "hi"}


def test_a_member_note_is_stored_through_the_shared_entry():
    thread, ctx = [], context()
    op = {"op": "add", "id": "n2", "thread": "notes", "by": "eng", "text": "Later"}
    assert ledger_comments.agent_thread_op(thread, op, ctx, "notes/n2", "note") is True
    assert thread == [{"id": "n2", "by": "eng", "at": 42, "text": "Later", "comments": []}]
    assert ctx.events == [(("eng", "note added", "notes/n2"), {"id": "n2", "text": "Later"})]
