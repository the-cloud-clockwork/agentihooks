from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

LEDGER = Path(__file__).parents[1] / "scripts" / "swarm_ledger"
AGENT = "engineer@1-1"
REFUSED = "^outcome rides only on an agent add to a comment thread, as done or blocked, without attachments$"


class Context:
    def __init__(self):
        self.at, self.meta, self.records = 5, {"members": {AGENT: {}}}, []

    def record(self, by, event, target, **fields):
        self.records.append((by, event, target, fields))


@pytest.fixture
def comments(monkeypatch):
    monkeypatch.syspath_prepend(str(LEDGER))
    from scripts.swarm_ledger import ledger_comments

    return ledger_comments


def post(comments, thread, ctx, entry_id, text, outcome=None):
    if outcome is None:
        comments.post_status(thread, AGENT, entry_id, text, ctx, "tasks/t")
    else:
        op = {"id": entry_id, "by": AGENT, "text": text, "outcome": outcome}
        comments.post_outcome(thread, op, ctx, "tasks/t")


def shown(thread):
    return [(entry["id"], entry["text"], entry.get("outcome")) for entry in thread]


def test_an_outcome_gets_its_own_entry_and_later_progress_never_amends_it(comments):
    thread, ctx = [], Context()
    post(comments, thread, ctx, "p1", "Building the first slice")
    post(comments, thread, ctx, "o1", "Outcome proposal: done. Pull request merged", "done")
    post(comments, thread, ctx, "p2", "Watching the merge queue")
    post(comments, thread, ctx, "p3", "Merge queue passed")
    post(comments, thread, ctx, "o2", "Outcome proposal: blocked. Waiting on a secret", "blocked")
    assert shown(thread) == [
        ("p1", "Building the first slice", None),
        ("o1", "Outcome proposal: done. Pull request merged", "done"),
        ("p2", "Merge queue passed", None),
        ("o2", "Outcome proposal: blocked. Waiting on a secret", "blocked"),
    ]
    assert [event for _, event, _, _ in ctx.records] == [
        "comment added",
        "comment added",
        "comment added",
        "comment edited",
        "comment added",
    ]
    assert ctx.meta["members"][AGENT]["last_seen"] == 5


def test_a_replayed_outcome_entry_is_not_added_twice(comments):
    thread, ctx = [], Context()
    text = "Outcome proposal: done. Pull request merged"
    post(comments, thread, ctx, "o1", text, "done")
    post(comments, thread, ctx, "o1", text, "done")
    assert thread == [{"id": "o1", "by": AGENT, "at": 5, "text": text, "outcome": "done"}]
    assert ctx.records == [(AGENT, "comment added", "tasks/t", {"id": "o1", "text": text})]
    assert ctx.meta["members"][AGENT]["last_seen"] == 5


def test_a_progress_line_marks_its_author_seen(comments):
    thread, ctx = [], Context()
    post(comments, thread, ctx, "p1", "Building the first slice")
    assert ctx.meta["members"][AGENT]["last_seen"] == 5


@pytest.mark.parametrize(
    ("entry", "by", "allowed"),
    [
        ({"by": AGENT, "text": "Building"}, AGENT, True),
        ({"by": AGENT, "text": "Building"}, "master@1-1", True),
        ({"by": AGENT, "text": "Building"}, "engineer@2-2", False),
        ({"by": "operator", "text": "Hold"}, "master@1-1", False),
        ({"by": AGENT, "text": "", "deleted": True}, AGENT, False),
        ({"by": AGENT, "text": "Merged", "outcome": "done"}, AGENT, False),
    ],
)
def test_who_may_change_an_entry(comments, entry, by, allowed):
    members = {AGENT: {}, "master@1-1": {"role": "orchestrator"}}
    assert comments.can_change(entry, by, members) is allowed


def test_an_agent_comment_add_with_an_outcome_takes_its_own_entry(comments):
    thread, ctx = [], Context()
    base = {"op": "add", "by": AGENT, "thread": "tasks/t/comments"}
    for op in (
        {**base, "id": "p1", "text": "Building the first slice"},
        {**base, "id": "o1", "text": "Outcome proposal: done. Pull request merged", "outcome": "done"},
        {**base, "id": "p2", "text": "Watching the merge queue"},
    ):
        assert comments.agent_thread_op(thread, op, ctx, "tasks/t", "comment")
    assert shown(thread) == [
        ("p1", "Building the first slice", None),
        ("o1", "Outcome proposal: done. Pull request merged", "done"),
        ("p2", "Watching the merge queue", None),
    ]
    assert {target for _, _, target, _ in ctx.records} == {"tasks/t"}


@pytest.mark.parametrize("outcome", ["done", "blocked"])
def test_an_agent_outcome_on_a_comment_add_is_accepted(comments, outcome):
    comments.check_outcome({"op": "add", "thread": "tasks/t/comments", "by": AGENT, "outcome": outcome})


def test_an_op_without_an_outcome_is_accepted(comments):
    comments.check_outcome({"op": "edit", "thread": "chat"})


@pytest.mark.parametrize(
    "op",
    [
        {"op": "add", "thread": "tasks/t/comments", "by": AGENT, "outcome": "merged"},
        {"op": "add", "thread": "tasks/t/comments", "by": AGENT, "outcome": None},
        {"op": "edit", "thread": "tasks/t/comments", "by": AGENT, "outcome": "done"},
        {"op": "add", "thread": "chat", "by": AGENT, "outcome": "done"},
        {"op": "add", "thread": "tasks/t/comments", "outcome": "done"},
        {"op": "add", "thread": "tasks/t/comments", "by": AGENT, "outcome": "done", "attachments": []},
    ],
)
def test_an_outcome_rides_only_on_an_agent_comment_add(comments, op):
    with pytest.raises(ValueError, match=REFUSED):
        comments.check_outcome(op)


@pytest.mark.parametrize("change", ["edit", "delete"])
def test_no_agent_edits_or_deletes_an_outcome_entry(comments, change):
    thread, ctx = [], Context()
    ctx.meta["members"]["master@1-1"] = {"role": "orchestrator"}
    post(comments, thread, ctx, "o1", "Outcome proposal: done. Pull request merged", "done")
    for by in (AGENT, "master@1-1"):
        op = {"op": change, "id": "o1", "by": by, "thread": "tasks/t/comments", "text": "Still building"}
        assert not comments.agent_thread_op(thread, op, ctx, "tasks/t", "comment")
    assert shown(thread) == [("o1", "Outcome proposal: done. Pull request merged", "done")]


@pytest.mark.parametrize(
    "op",
    [
        {"op": "add", "thread": "tasks/t/comments"},
        {"op": "add", "thread": "notes", "by": AGENT},
        {"op": "edit", "thread": "tasks/t/comments", "by": AGENT},
    ],
)
def test_the_ledger_op_check_refuses_a_misplaced_outcome(comments, op):
    from scripts.swarm_ledger import ledger_core

    with pytest.raises(ValueError, match=REFUSED):
        ledger_core.check_op({**op, "id": "o1", "text": "Pull request merged", "outcome": "done"})


def test_the_ledger_op_check_accepts_an_agent_outcome_on_a_comment_add(comments):
    from scripts.swarm_ledger import ledger_core

    op = {"op": "add", "id": "o1", "thread": "tasks/t/comments", "by": AGENT, "text": "Pull request merged"}
    ledger_core.check_op({**op, "outcome": "blocked"})
