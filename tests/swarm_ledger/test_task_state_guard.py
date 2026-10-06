import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts" / "swarm_ledger"))

from scripts.swarm_ledger import ledger_tasks  # noqa: E402
from scripts.swarm_ledger.ledger_core import Context  # noqa: E402

PR = "https://github.com/o/r/pull/2"
REOPEN = {"state": "open", "claimed_by": ""}


def ledger(state):
    task = {"id": "t1", "title": "a", "lane": "eng", "kind": "code", "state": state, "claimed_by": "eng-1"}
    return {"tasks": [{**task, "pr_url": PR, "done": state == "done"}]}


def update(doc, fields, if_state):
    op = {"op": "task_update", "id": "u1", "by": "swarm", "item": "tasks/t1", "fields": fields, "if_state": if_state}
    ledger_tasks.check(op)
    ctx = Context({"rev": 0, "stamps": {}}, 1)
    return ledger_tasks.apply(doc, op, ctx), ctx


def test_a_guarded_reopen_leaves_a_done_task_done_and_is_not_refused():
    doc = ledger("done")
    applied, ctx = update(doc, REOPEN, ["claimed", "pr"])
    assert applied is True
    assert (doc["tasks"][0]["state"], doc["tasks"][0]["claimed_by"], doc["tasks"][0]["done"]) == ("done", "eng-1", True)
    assert (ctx.events, ctx.refused, ctx.dirty, ctx.stamps) == ([], [], False, {})


def test_a_guarded_reopen_applies_while_the_state_is_listed():
    doc = ledger("pr")
    applied, ctx = update(doc, REOPEN, ["claimed", "pr"])
    assert applied is True
    assert (doc["tasks"][0]["state"], doc["tasks"][0]["claimed_by"]) == ("open", "")
    assert [(e["kind"], e["target"]) for e in ctx.events] == [("task open", "tasks/t1")]


@pytest.mark.parametrize("if_state", [["finished"], "pr", [1], [{"state": "pr"}]])
def test_a_guard_naming_no_task_state_is_refused(if_state):
    with pytest.raises(ValueError, match="if_state"):
        update(ledger("pr"), REOPEN, if_state)


@pytest.mark.parametrize(("if_state", "after"), [(["open"], "claimed"), (["pr"], None)])
def test_a_task_with_no_state_is_guarded_as_open(if_state, after):
    doc = ledger("open")
    del doc["tasks"][0]["state"]
    update(doc, {"state": "claimed", "claimed_by": "eng-2"}, if_state)
    assert doc["tasks"][0].get("state") == after
