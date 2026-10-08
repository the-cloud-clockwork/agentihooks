import pytest

from scripts.swarm_ledger import ledger_core as core
from scripts.swarm_ledger import ledger_phases, ledger_priorities
from tests.swarm_ledger.plan_slices import anchored
from tests.swarm_ledger.test_phases import SLUG, make_ledger


@pytest.fixture(autouse=True)
def review_ledger_dir(ledger_dir, monkeypatch):
    monkeypatch.setattr(core, "LEDGER_DIR", ledger_dir)


def sync(*ops):
    for op in ops:
        core.check_op(op)
    return core.sync(SLUG, ops=list(ops))


def task_add(task, lane="eng", kind="code"):
    return {"op": "task_add", "id": f"add-{task}", "by": "planner", "task": task, "title": f"Task {task}",
            "lane": lane, "kind": kind, "phase": "p1", "plan_url": "https://github.com/acme/app/issues/1",
            **({"plan_slice": task} if kind == "code" else {})}  # fmt: skip


def planned():
    make_ledger()
    anchored(SLUG, "t1")
    done = {"state": "done", "claimed_by": "planner", "proof": {"slice": "t1"}}
    update = {"op": "task_update", "id": "plan-done", "by": "planner", "item": "tasks/plan-p1", "fields": done}
    state, rejected = sync(task_add("t1"), task_add("plan-p1", "plan", "plan"), update)
    assert rejected == []
    return state


def review(state, by="master", n=0, **fields):
    return {"op": "phase_review", "id": f"review-{state}-{n}", "by": by, "item": "phases/p1", "state": state, **fields}


def plan_task(state):
    return next(t for t in state["tasks"] if t["id"] == "plan-p1")


def test_send_back_reopens_the_plan_task_and_counts_the_round():
    planned()
    state, rejected = sync(review("sent_back", note="Split the parser task"))
    assert rejected == []
    record = state["phases"][0]["review"]
    assert (record["state"], record["rounds"], record["note"]) == ("sent_back", 1, "Split the parser task")
    assert record["notes"] == ["Split the parser task"] and record["escalated"] is False
    plan = plan_task(state)
    assert (plan["state"], plan["claimed_by"], plan["done"]) == ("open", "", False)
    assert next(t for t in state["tasks"] if t["id"] == "t1")["state"] == "open"
    [event] = [e for e in state["_meta"]["events"] if e["kind"] == "task open"]
    assert (event["by"], event["target"]) == ("master", "tasks/plan-p1")
    assert state["_meta"]["stamps"]["tasks/plan-p1/state"]["by"] == "master"


def test_send_back_of_an_open_plan_task_records_no_reopen():
    planned()
    sync(review("sent_back", note="First note"))
    state, rejected = sync(review("sent_back", n=1, note="Second note"))
    assert rejected == [] and state["phases"][0]["review"]["rounds"] == 2
    assert len([e for e in state["_meta"]["events"] if e["kind"] == "task open"]) == 1


def test_a_new_pending_review_keeps_rounds_notes_and_the_phase_ask():
    planned()
    ask = {"op": "priority", "id": "ask-p1", "by": "swarm", "item": "phases/p1", "text": "Approve the slice."}
    sync(review("sent_back", note="First note"))
    state, rejected = sync(ask, review("pending", by="swarm"))
    assert rejected == []
    record = state["phases"][0]["review"]
    assert (record["state"], record["rounds"], record["notes"]) == ("pending", 1, ["First note"])
    assert "escalated" not in record
    assert [p["text"] for p in state["priorities"] if p["item"] == "phases/p1"] == ["Approve the slice."]


def test_third_send_back_escalates_without_reopening_and_asks_the_operator():
    planned()
    for n in (1, 2):
        sync(review("sent_back", n=n, note=f"Note {n}"))
        reclaim = {"op": "task_update", "id": f"redo-{n}", "by": "planner", "item": "tasks/plan-p1",
                   "fields": {"state": "done"}}  # fmt: skip
        sync(reclaim, review("pending", by="swarm", n=n))
    state, rejected = sync(review("sent_back", n=3, note="Note 3"))
    assert rejected == []
    record = state["phases"][0]["review"]
    assert (record["rounds"], record["escalated"], record["notes"]) == (3, True, ["Note 1", "Note 2", "Note 3"])
    assert plan_task(state)["state"] == "done"
    [ask] = [p for p in state["priorities"] if p["item"] == "phases/p1"]
    assert ask["text"] == "Decide the plan, sent back 3 times: Note 1; Note 2; Note 3"
    state, rejected = sync(review("sent_back", by="operator", n=4, note="One more try"))
    assert rejected == []
    record = state["phases"][0]["review"]
    assert (record["rounds"], record["escalated"]) == (4, False)
    assert plan_task(state)["state"] == "open"
    assert not [p for p in state["priorities"] if p["item"] == "phases/p1"]


def test_approve_keeps_the_rounds_and_clears_only_the_phase_ask():
    planned()
    ask = {"op": "priority", "id": "ask-p1", "by": "swarm", "item": "phases/p1", "text": "Approve the slice."}
    other = {"op": "priority", "id": "ask-t1", "by": "swarm", "item": "tasks/t1", "text": "Look at this task."}
    sync(ask, other, review("sent_back", note="Fix it"))
    sync(ask)
    state, rejected = sync(review("approved", by="operator", note="Looks right"))
    assert rejected == []
    record = state["phases"][0]["review"]
    assert (record["state"], record["rounds"], record["notes"]) == ("approved", 1, ["Fix it"])
    assert plan_task(state)["state"] == "open"
    assert [p["item"] for p in state["priorities"]] == ["tasks/t1"]


def test_send_back_without_a_plan_task_writes_only_the_record():
    make_ledger()
    state, rejected = sync(review("sent_back", note="Nothing planned yet"))
    assert rejected == []
    assert state["phases"][0]["review"]["rounds"] == 1 and state["tasks"] == []


@pytest.mark.parametrize(
    "fields, message",
    [
        ({}, "a send back needs a note"),
        ({"note": "  "}, "a send back needs a note"),
        ({"note": "n", "rounds": 2}, "a send back counts its own rounds"),
        ({"note": "n", "escalated": True}, "a send back counts its own rounds"),
    ],
)
def test_send_back_validation(fields, message):
    with pytest.raises(ValueError) as refused:
        ledger_phases.check(review("sent_back", **fields))
    assert str(refused.value) == message


OVERRIDE = {"reason": "Accepted as is", "problems": ["Task t1 names no territory."]}


@pytest.mark.parametrize(
    "state, override, message",
    [
        ("sent_back", OVERRIDE, "only an approval carries an override"),
        ("pending", OVERRIDE, "only an approval carries an override"),
        ("approved", "Accepted", "an override needs a reason and the problems it overrode"),
        ("approved", {"problems": ["x"]}, "an override needs a reason and the problems it overrode"),
        ("approved", {**OVERRIDE, "reason": " "}, "an override needs a reason and the problems it overrode"),
        ("approved", {**OVERRIDE, "problems": []}, "an override needs a reason and the problems it overrode"),
        ("approved", {**OVERRIDE, "problems": [1]}, "an override needs a reason and the problems it overrode"),
        ("approved", {**OVERRIDE, "problems": "x"}, "an override needs a reason and the problems it overrode"),
        ("approved", {**OVERRIDE, "by": "me"}, "an override needs a reason and the problems it overrode"),
    ],
)
def test_override_validation(state, override, message):
    with pytest.raises(ValueError) as refused:
        ledger_phases.check(review(state, note="n", override=override))
    assert str(refused.value) == message


def test_an_override_lands_on_the_approval_record_only():
    planned()
    state, rejected = sync(review("approved", override=OVERRIDE))
    assert rejected == []
    assert state["phases"][0]["review"]["override"] == OVERRIDE
    state, rejected = sync(review("sent_back", n=1, note="Again"))
    assert rejected == [] and "override" not in state["phases"][0]["review"]


def test_round_cap_is_three():
    assert ledger_phases.ROUND_CAP == 3


ESCALATED = {"state": "sent_back", "rounds": 3, "escalated": True, "notes": ["Split it", "Name the done condition"]}


@pytest.mark.parametrize(
    "review, extra, asked",
    [
        (ESCALATED, {}, "Decide the plan, sent back 3 times: Split it; Name the done condition"),
        ({**ESCALATED, "escalated": False}, {}, None),
        ({**ESCALATED, "state": "approved"}, {}, None),
        (ESCALATED, {"out_of_scope": True}, None),
        ({**ESCALATED, "rounds": 4, "notes": [" ".join(["word"] * 20)]}, {},
         "Decide the plan, sent back 4 times: " + " ".join(["word"] * 16) + "…"),
    ],
)  # fmt: skip
def test_an_escalated_review_asks_the_operator_naming_the_notes(review, extra, asked):
    doc = {"phases": [{"id": "p1", "title": "Build", "review": review, **extra}, {"id": "p2", "title": "Later"}]}
    assert ledger_priorities.wanted(doc).get("phases/p1") == asked
    assert "phases/p2" not in ledger_priorities.wanted(doc)
