from types import SimpleNamespace

import pytest

from scripts.swarm_ledger import ledger_authority, ledger_comments, ledger_core, ledger_notifications
from scripts.swarm_ledger.api import schemas

TO_ONLY_CHAT = "to addresses only an agent chat message or comment, reply_to only an agent chat message"
OPERATOR_ONLY = "an agent chat message goes to the operator or answers his line by its id"


def op(thread="chat", **fields):
    return {"op": "add", "thread": thread, "id": "m-1", "text": "Phase one is done.", "by": "eng", **fields}


@pytest.mark.parametrize(
    "sent",
    [
        op("phases/p1/comments", reply_to="m-0"),
        {key: value for key, value in op("phases/p1/comments", to="operator").items() if key != "by"},
        {key: value for key, value in op("notes", to="operator").items() if key != "by"},
        {**op("phases/p1/comments", to="operator"), "op": "edit"},
    ],
)
def test_an_address_rides_only_on_an_agent_chat_message_or_an_answer_comment(sent):
    with pytest.raises(ValueError) as refused:
        ledger_core.check_op(sent)
    assert str(refused.value) == TO_ONLY_CHAT


def test_an_agent_comment_to_the_operator_passes_the_check():
    assert ledger_core.check_op(op("phases/p1/comments", to="operator")) is None


ANSWER = {"id": "m-1", "by": "eng", "at": 7, "text": "Phase one is done.", "to": "operator"}


def test_an_agent_answer_to_the_operator_is_a_new_comment_beside_its_status():
    status = {"id": "c-0", "by": "eng", "at": 1, "text": "Working on the phase."}
    thread, c = [dict(status)], ctx()
    c.meta["members"] = {"eng": {"last_seen": 1}}
    c.record = lambda *args, **fields: c.records.append((args, fields))
    answered = op("phases/p1/comments", to="operator")
    assert ledger_comments.agent_thread_op(thread, answered, c, "phases/p1", "comment") is True
    assert thread == [status, ANSWER] and c.meta["members"]["eng"]["last_seen"] == 7
    assert c.records == [(("eng", "comment added", "phases/p1"), {"id": "m-1", "text": "Phase one is done."})]
    assert ledger_comments.agent_thread_op(thread, answered, c, "phases/p1", "comment") is True
    assert len(thread) == 2 and len(c.records) == 1


def test_only_the_swarm_relay_answers_the_operator_on_an_entry():
    answered = op("phases/p1/comments", to="operator")
    assert ledger_authority.refusal("eng", answered) == (
        "eng cannot answer the operator on an entry; the swarm relay posts that answer"
    )
    assert ledger_authority.refusal("", answered) == ""
    assert ledger_authority.refusal("eng", op("chat", to="operator")) == ""
    assert ledger_authority.refusal("eng", op("phases/p1/comments")) == ""


def test_an_agent_status_after_its_answer_leaves_the_answer_intact():
    thread, c = [dict(ANSWER)], ctx()
    status = {**op("phases/p1/comments"), "id": "c-2", "text": "Still on the phase."}
    assert ledger_comments.agent_thread_op(thread, status, c, "phases/p1", "comment") is True
    assert thread == [ANSWER, {"id": "c-2", "by": "eng", "at": 7, "text": "Still on the phase."}]


def test_an_answer_does_not_count_against_the_one_status_rule():
    item = {"comments": [{"id": "c-0", "by": "eng", "text": "Working on the phase."}, dict(ANSWER)]}
    assert ledger_comments.audit_item("phases/p1", item) == []
    item["comments"].append({"id": "c-3", "by": "eng", "text": "Another status."})
    assert ledger_comments.audit_item("phases/p1", item) == [("phases/p1", "-", "eng", ["2 comments, keep one status"])]


def test_consecutive_status_comments_are_flagged():
    item = {
        "comments": [
            {"id": "c-1", "by": "eng", "text": "Working on the phase."},
            {"id": "c-2", "by": "eng", "text": "The change is ready."},
        ]
    }
    assert ledger_comments.audit_item("phases/p1", item) == [("phases/p1", "-", "eng", ["2 comments, keep one status"])]


def test_status_comments_separated_by_another_author_are_not_flagged():
    for author in ("other", "operator"):
        item = {
            "comments": [
                {"id": "c-1", "by": "eng", "text": "Working on the phase."},
                {"id": "c-2", "by": author, "text": "The review is ready."},
                {"id": "c-3", "by": "eng", "text": "The change is ready."},
            ]
        }
        assert ledger_comments.audit_item("phases/p1", item) == []


def test_deleted_comment_does_not_interrupt_consecutive_statuses():
    item = {
        "comments": [
            {"id": "c-1", "by": "eng", "text": "Working on the phase."},
            {"id": "c-2", "by": "other", "text": "The review is ready.", "deleted": True},
            {"id": "c-3", "by": "eng", "text": "The change is ready."},
        ]
    }
    assert ledger_comments.audit_item("phases/p1", item) == [("phases/p1", "-", "eng", ["2 comments, keep one status"])]


def test_consecutive_operator_comments_are_not_repeated_agent_statuses():
    item = {
        "comments": [
            {"id": "c-1", "by": "operator", "text": "The review is ready."},
            {"id": "c-2", "by": "operator", "text": "The change is ready."},
        ]
    }
    assert ledger_comments.audit_item("phases/p1", item) == []


def test_an_agent_status_comment_still_amends_its_last_one():
    thread, c = [{"id": "c-0", "by": "eng", "at": 1, "text": "Working on the phase."}], ctx()
    assert ledger_comments.agent_thread_op(thread, op("phases/p1/comments"), c, "phases/p1", "comment") is True
    assert [(e["id"], e["text"]) for e in thread] == [("c-0", "Phase one is done.")]


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


@pytest.mark.parametrize(
    ("fields", "reply"),
    [({"to": "operator"}, {}), ({"reply_to": "m-0"}, {"reply_to": "m-0"})],
)
def test_an_addressed_agent_message_is_kept(fields, reply):
    thread, c = [{"id": "m-0", "by": "operator", "text": "hi"}], ctx()
    assert ledger_comments.agent_thread_op(thread, op(**fields), c, "chat", "message") is True
    assert c.refused == []
    assert thread[-1] == {"id": "m-1", "by": "eng", "at": 7, "text": "Phase one is done.", **reply}


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
