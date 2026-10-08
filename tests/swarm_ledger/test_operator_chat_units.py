from types import SimpleNamespace

import pytest

from scripts.swarm_ledger import ledger_comments, ledger_core, ledger_notifications
from scripts.swarm_ledger.api import schemas

TO_ONLY_CHAT = "to and reply_to address only an agent chat message"
OPERATOR_ONLY = "an agent chat message goes to the operator or answers his line by its id"


def op(thread="chat", **fields):
    return {"op": "add", "thread": thread, "id": "m-1", "text": "Phase one is done.", "by": "eng", **fields}


@pytest.mark.parametrize("field", [{"to": "operator"}, {"reply_to": "m-0"}])
def test_an_address_rides_only_on_an_agent_chat_message(field):
    with pytest.raises(ValueError) as refused:
        ledger_core.check_op(op("phases/p1/comments", **field))
    assert str(refused.value) == TO_ONLY_CHAT


@pytest.mark.parametrize("field", [{"to": "operator"}, {"reply_to": "m-0"}, {}])
def test_an_agent_chat_message_to_the_operator_or_his_line_passes_the_check(field):
    assert ledger_core.check_op(op(**field)) is None


@pytest.mark.parametrize("field", [{"to": "eng"}, {"reply_to": 5}])
def test_an_agent_chat_message_to_anyone_else_is_refused(field):
    with pytest.raises(ValueError) as refused:
        ledger_core.check_op(op(**field))
    assert str(refused.value) == OPERATOR_ONLY


@pytest.mark.parametrize(
    "fields, thread, expected",
    [
        ({"to": "operator"}, [], True),
        ({"reply_to": "m-0"}, [{"id": "m-0", "by": "operator"}], True),
        ({"reply_to": "m-0"}, [{"id": "m-0", "by": "eng"}], False),
        ({"reply_to": "m-0"}, [{"id": "m-0", "by": "operator", "deleted": True}], False),
        ({"reply_to": "m-9"}, [{"id": "m-0", "by": "operator"}], False),
        ({}, [{"id": "m-0", "by": "operator"}], False),
        ({"to": "eng"}, [], False),
    ],
)
def test_a_message_is_addressed_only_to_the_operator_or_a_live_line_of_his(fields, thread, expected):
    assert ledger_comments.addressed(thread, fields) is expected


def ctx():
    return SimpleNamespace(meta={}, refused=[], at=7, records=[], record=lambda *a, **k: None)


def test_an_unaddressed_agent_message_is_refused_and_not_kept():
    thread, c = [{"id": "m-0", "by": "operator", "text": "hi"}], ctx()
    assert ledger_comments.agent_thread_op(thread, op(), c, "chat", "message") is False
    assert c.refused == [f"eng message refused: {ledger_comments.UNADDRESSED}"]
    assert [e["id"] for e in thread] == ["m-0"]


@pytest.mark.parametrize("fields", [{"to": "operator"}, {"reply_to": "m-0"}])
def test_an_addressed_agent_message_is_kept(fields):
    thread, c = [{"id": "m-0", "by": "operator", "text": "hi"}], ctx()
    assert ledger_comments.agent_thread_op(thread, op(**fields), c, "chat", "message") is True
    assert c.refused == []
    assert thread[-1] == {"id": "m-1", "by": "eng", "at": 7, "text": "Phase one is done."}


def notice(**fields):
    return {"op": "notice", "id": "n-1", "by": "swarm", "text": "The swarm is paused.", **fields}


def test_a_swarm_notice_passes_the_check():
    assert ledger_notifications.check(notice()) is None


@pytest.mark.parametrize("bad", [notice(item="chat"), notice(text="  "), notice(text=5)])
def test_a_notice_takes_only_id_by_and_text(bad):
    with pytest.raises(ValueError) as refused:
        ledger_notifications.check(bad)
    assert str(refused.value) == "notice takes only id, by and text, for the operator's notifications panel"


@pytest.mark.parametrize("by", ["operator", "two words"])
def test_a_notice_comes_from_the_swarm_or_an_agent(by):
    with pytest.raises(ValueError) as refused:
        ledger_notifications.check(notice(by=by))
    assert str(refused.value) == "a notice comes from the swarm or an agent; the operator talks in chat"


def test_a_notice_lands_in_the_panel_with_no_item_and_kept_text():
    doc, c, text = {}, SimpleNamespace(rev=3, at=9, dirty=False), "x" * 300
    assert ledger_notifications.apply(doc, notice(text=text), c) is True
    assert doc["notifications"] == [
        {"id": "nt-3-n-1", "item": "", "label": "Swarm notice", "text": "x" * 280, "by": "swarm", "at": 9}
    ]
    assert c.dirty is True


def test_a_notice_targets_the_notifications():
    assert schemas.target(notice()) == "notifications"
