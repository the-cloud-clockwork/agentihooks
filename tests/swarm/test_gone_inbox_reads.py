import json
from dataclasses import replace

import pytest

from scripts.inbox.store import InboxError, InboxStore, replied
from scripts.swarm import affinity, automated_gc, doc_report, master_move
from scripts.swarm.store import RedisStore


@pytest.fixture
def store():
    import fakeredis

    return RedisStore(fakeredis.FakeRedis(decode_responses=True))


def gone_line(by, item_id):
    return f"agentihooks inbox: {by} holds message {item_id} whose record is gone; skipped it"


def test_find_returns_a_stored_item_and_none_with_one_log_line_for_a_gone_one(store, capsys):
    inbox = InboxStore(store.redis)
    kept = inbox.send("alice", "bob", "here")
    assert inbox.find(kept.id, "pass") == kept
    assert capsys.readouterr().err == ""
    assert inbox.find("gone1", "pass") is None
    assert capsys.readouterr().err.splitlines() == [gone_line("pass", "gone1")]
    with pytest.raises(InboxError, match="^no message gone1$"):
        inbox.get("gone1")


@pytest.mark.parametrize(
    ("module", "key", "by"),
    [(affinity, "master-affinity", "master-affinity"), (master_move, "master-move", "master-move")],
)
def test_a_master_order_whose_item_is_gone_is_skipped(store, capsys, module, key, by):
    store.redis.set(store.key("sw", key), json.dumps({"item": "gone1", "to": "", "master": ""}))
    (module.handed_off if module is affinity else module.placed)(store, "sw")
    assert capsys.readouterr().err.splitlines() == [gone_line(by, "gone1")]


def test_a_gc_probe_whose_request_or_answer_is_gone_counts_no_answer(store, capsys):
    inbox = InboxStore(store.redis)
    assert automated_gc._answer(inbox, None, {"request": "gone1", "answers": []}, 1) is False
    request = inbox.send("swarm", "bob", "still there?")
    inbox.close(request.id, "bob", "done", "replied with message gone2")
    record = {"request": request.id, "answers": []}
    assert automated_gc._answer(inbox, None, record, 1) is False
    assert record["answers"] == []
    assert capsys.readouterr().err.splitlines() == [
        gone_line("automated-gc", "gone1"),
        gone_line("automated-gc", "gone2"),
    ]


def test_a_doc_report_answer_whose_reply_is_gone_keeps_the_close_reason(store, capsys):
    inbox = InboxStore(store.redis)
    item = replace(inbox.send("swarm", "bob", "report"), state="done", reason=f"done: {replied('gone1')}")
    assert doc_report._answer(inbox, item) == replied("gone1")
    assert capsys.readouterr().err.splitlines() == [gone_line(doc_report.FIELD, "gone1")]
