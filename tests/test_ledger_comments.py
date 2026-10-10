from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

LEDGER = Path(__file__).parents[1] / "scripts" / "swarm_ledger"
AGENT = "engineer@1-1"


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
    comments.post_status(thread, AGENT, entry_id, text, ctx, "tasks/t", outcome=outcome)


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
    post(comments, thread, ctx, "o1", "Outcome proposal: done. Pull request merged", "done")
    post(comments, thread, ctx, "o1", "Outcome proposal: done. Pull request merged", "done")
    assert shown(thread) == [("o1", "Outcome proposal: done. Pull request merged", "done")]
    assert len(ctx.records) == 1


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
    ],
)
def test_an_outcome_rides_only_on_an_agent_comment_add(comments, op):
    with pytest.raises(ValueError, match="outcome rides only on an agent add to a comment thread, as done or blocked"):
        comments.check_outcome(op)
