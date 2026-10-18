from types import SimpleNamespace

import pytest

from scripts.swarm import ledger_client


@pytest.fixture
def sent(monkeypatch):
    ops = []

    def call(slug, batch, service):
        ops.extend((slug, op) for op in batch)
        return {"rejected": []}

    monkeypatch.setattr(ledger_client, "_ledger", lambda: SimpleNamespace(call=call))
    return ops


def without_id(sent):
    return [(slug, {k: v for k, v in op.items() if k != "id"}) for slug, op in sent]


def test_clear_priority_sends_its_reason_as_the_swarm(sent):
    ledger_client.LedgerClient().clear_priority("demo", "pr-1", "its item is done")
    assert without_id(sent) == [
        ("demo", {"op": "priority_clear", "by": "swarm", "target": "pr-1", "reason": "its item is done"})
    ]
    assert sent[0][1]["id"].startswith("priority_clear-")


def test_comment_item_posts_on_the_item_thread(sent):
    ledger_client.LedgerClient().comment_item("demo", "followups/f1", "Cleared.")
    assert without_id(sent) == [
        ("demo", {"op": "add", "by": "swarm", "thread": "followups/f1/comments", "text": "Cleared."})
    ]


def test_mark_done_sets_the_done_flag(sent):
    ledger_client.LedgerClient().mark_done("demo", "followups/f1")
    assert without_id(sent) == [("demo", {"op": "set", "by": "swarm", "path": "followups/f1/done", "value": True})]


def test_answer_as_operator_carries_no_author(sent):
    ledger_client.LedgerClient().answer_as_operator("demo", "questions/q1", "Use the small one.")
    assert without_id(sent) == [("demo", {"op": "add", "thread": "questions/q1/answers", "text": "Use the small one."})]


@pytest.mark.parametrize("flag", [{}, {"needs_operator": True}])
def test_followup_flags_the_item_for_the_operator_only_when_asked(sent, flag):
    ledger_client.LedgerClient().followup("demo", "Pick a port.", **flag)
    [(slug, op)] = without_id(sent)
    op.pop("text")
    assert (slug, op) == ("demo", {"op": "add_item", "by": "swarm", "list": "followups", **flag})
