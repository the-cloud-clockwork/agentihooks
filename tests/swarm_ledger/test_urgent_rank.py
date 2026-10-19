import pytest

from scripts.swarm_ledger import ledger_core as core
from scripts.swarm_ledger import ledger_freezes, ledger_rank, ledger_tasks, new_ledger
from tests.swarm_ledger import legacy_page

SLUG = "urgentrank-2026-01-01"
MASTER = "master@abcdef-0001"
WAITING = "task t2 cannot be urgent: its phase two is waiting on an earlier phase"
FROZEN = "task t1 cannot be urgent: the freeze on phase one holds it"
HINT = "; move it to a phase that admits it or lift the hold first"


@pytest.fixture(autouse=True)
def ledger_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(core, "LEDGER_DIR", tmp_path)
    monkeypatch.setitem(core.EXTENSION_OPS, "task_add", ledger_tasks)
    monkeypatch.setitem(core.EXTENSION_OPS, "task_update", ledger_tasks)
    monkeypatch.setitem(core.EXTENSION_OPS, "task_rank", ledger_rank)
    monkeypatch.setitem(core.EXTENSION_OPS, "freeze_set", ledger_freezes)
    phases = [{"title": "one", "description": "d"}, {"title": "two", "description": "d", "depends_on": [1]}]
    content = {"title": "Demo", "overview": "o", "sources": [], "phases": phases}
    html_path, _ = legacy_page.paths(SLUG, core)
    html_path.write_text(legacy_page.render(new_ledger.build_doc(content), SLUG, 8765), encoding="utf-8")
    seeds = [
        {"op": "task_add", "id": "seed-1", "by": "swarm", "task": "t1", "title": "a", "lane": "eng", "phase": "p1"},
        {"op": "task_add", "id": "seed-2", "by": "swarm", "task": "t2", "title": "b", "lane": "eng", "phase": "p2"},
    ]
    core.sync(SLUG, ops=seeds)


def rank_of(state, task_id):
    return next(t for t in state["tasks"] if t["id"] == task_id).get("rank")


def sync(op):
    core.check_op(op)
    return core.sync(SLUG, ops=[op])


def update(task_id, fields, n=1):
    return sync({"op": "task_update", "id": f"up-{n}", "by": MASTER, "item": f"tasks/{task_id}", "fields": fields})


def freeze_phase_one():
    return sync({"op": "freeze_set", "id": "fz-1", "verb": "freeze", "target": "phases/p1", "reason": "hold"})


@pytest.mark.parametrize("rank", ["urgent", "next"])
def test_the_master_cannot_make_a_task_in_a_waiting_phase_urgent(rank):
    state, rejected = update("t2", {"rank": rank})
    assert (rejected, rank_of(state, "t2")) == (["up-1"], None)
    assert state["_meta"]["warnings"][-1] == WAITING + HINT


def test_a_high_rank_in_a_waiting_phase_still_lands():
    state, rejected = update("t2", {"rank": "high"})
    assert (rejected, rank_of(state, "t2")) == ([], "high")


def test_the_page_rank_op_cannot_make_a_task_in_a_waiting_phase_urgent():
    state, rejected = sync({"op": "task_rank", "id": "pr-1", "item": "tasks/t2", "rank": "urgent"})
    assert (rejected, rank_of(state, "t2")) == (["pr-1"], None)
    assert state["_meta"]["warnings"][-1] == WAITING + HINT


def test_an_urgent_rank_in_a_building_phase_lands():
    state, rejected = update("t1", {"rank": "urgent"})
    assert (rejected, rank_of(state, "t1")) == ([], "urgent")
    state, rejected = sync({"op": "task_rank", "id": "pr-2", "item": "tasks/t1", "rank": "urgent", "by": MASTER})
    assert (rejected, rank_of(state, "t1")) == ([], "urgent")


def test_a_frozen_task_cannot_be_made_urgent_and_the_refusal_names_the_freeze():
    _, rejected = freeze_phase_one()
    assert rejected == []
    state, rejected = update("t1", {"rank": "urgent"})
    assert (rejected, rank_of(state, "t1")) == (["up-1"], None)
    assert state["_meta"]["warnings"][-1] == FROZEN + HINT


def test_an_urgent_task_cannot_move_into_a_waiting_phase():
    update("t1", {"rank": "urgent"})
    state, rejected = update("t1", {"phase": "p2"}, 2)
    assert rejected == ["up-2"]
    assert next(t for t in state["tasks"] if t["id"] == "t1")["phase"] == "p1"
    assert (
        state["_meta"]["warnings"][-1]
        == "task t1 cannot be urgent: its phase two is waiting on an earlier phase" + HINT
    )


def test_a_task_moving_out_of_a_waiting_phase_may_become_urgent_in_one_update():
    state, rejected = update("t2", {"rank": "urgent", "phase": "p1"})
    assert (rejected, rank_of(state, "t2")) == ([], "urgent")


def test_a_claimed_task_in_a_waiting_phase_may_still_be_made_urgent():
    update("t2", {"state": "claimed", "claimed_by": "engineer@abcdef-0002"})
    state, rejected = update("t2", {"rank": "urgent"}, 2)
    assert (rejected, rank_of(state, "t2")) == ([], "urgent")


def test_an_unrelated_update_to_a_held_urgent_task_still_lands():
    update("t1", {"rank": "urgent"})
    freeze_phase_one()
    state, rejected = update("t1", {"description": "renamed"}, 2)
    assert rejected == []
    assert next(t for t in state["tasks"] if t["id"] == "t1")["description"] == "renamed"


def test_an_urgent_rank_outside_the_focus_lands_because_urgent_tasks_skip_the_focus():
    _, rejected = sync({"op": "freeze_set", "id": "fc-1", "verb": "focus", "target": "phases/p2", "reason": "rush"})
    assert rejected == []
    state, rejected = update("t1", {"rank": "urgent"})
    assert (rejected, rank_of(state, "t1")) == ([], "urgent")


def test_an_urgent_task_add_into_a_waiting_phase_is_refused_and_one_without_a_phase_lands():
    add = {"op": "task_add", "by": MASTER, "title": "c", "lane": "eng", "rank": "urgent"}
    state, rejected = sync({**add, "id": "add-1", "task": "t3", "phase": "p2"})
    assert rejected == ["add-1"] and "t3" not in [t["id"] for t in state["tasks"]]
    assert (
        state["_meta"]["warnings"][-1]
        == "task t3 cannot be urgent: its phase two is waiting on an earlier phase" + HINT
    )
    state, rejected = sync({**add, "id": "add-2", "task": "t4"})
    assert (rejected, rank_of(state, "t4")) == ([], "urgent")


def test_a_relayed_operator_urgent_rank_is_refused_in_a_waiting_phase_too():
    from hooks.context import operator_words

    operator_words.record(MASTER, "Make the quota fix urgent right now please")
    core.sync(SLUG, ops=[{"op": "join", "id": "j1", "by": MASTER, "role": "orchestrator"}])
    op = {"op": "task_update", "id": "rel-1", "by": MASTER, "item": "tasks/t2", "fields": {"rank": "urgent"}}
    state, rejected = sync({**op, "quote": "make the quota fix urgent"})
    assert (rejected, rank_of(state, "t2")) == (["rel-1"], None)
    assert state["_meta"]["warnings"][-1] == WAITING + HINT
