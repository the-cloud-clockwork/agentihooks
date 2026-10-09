import pytest

from scripts.swarm_ledger import ledger_core as core
from scripts.swarm_ledger import ledger_tasks, new_ledger

SLUG = "taskphase-2026-01-01"
MASTER = "master@abcdef-0001"
ENGINEER = "engineer@abcdef-0002"
PLANNER = "planner@abcdef-0003"


@pytest.fixture(autouse=True)
def ledger_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(core, "LEDGER_DIR", tmp_path)
    monkeypatch.setitem(core.EXTENSION_OPS, "task_add", ledger_tasks)
    monkeypatch.setitem(core.EXTENSION_OPS, "task_update", ledger_tasks)
    phases = [{"title": "one", "description": "d"}, {"title": "two", "description": "e"}]
    content = {"title": "Demo", "overview": "o", "sources": [], "phases": phases}
    html_path, _ = core.paths(SLUG)
    html_path.write_text(new_ledger.render(new_ledger.build_doc(content), SLUG, 8765), encoding="utf-8")
    add = {"op": "task_add", "id": "seed", "by": "swarm", "task": "t1", "title": "a", "lane": "eng", "phase": "p1"}
    core.sync(SLUG, ops=[add])


def move(by, phase):
    op = {"op": "task_update", "id": "move-1", "by": by, "item": "tasks/t1", "fields": {"phase": phase}}
    core.check_op(op)
    return core.sync(SLUG, ops=[op])


def phase_of(state):
    return next(t for t in state["tasks"] if t["id"] == "t1")["phase"]


@pytest.mark.parametrize("by", [MASTER, PLANNER])
def test_a_planning_seat_moves_a_task_to_another_phase_and_it_is_recorded(by):
    state, rejected = move(by, "p2")
    assert (rejected, phase_of(state)) == ([], "p2")
    assert state["_meta"]["stamps"]["tasks/t1/phase"]["by"] == by
    event = state["_meta"]["events"][-1]
    assert (event["by"], event["kind"], event["target"], event["text"]) == (by, "task moved", "tasks/t1", "p1 to p2")


def test_a_task_with_no_phase_is_moved_into_one():
    core.sync(SLUG, ops=[{"op": "task_add", "id": "seed-2", "by": "swarm", "task": "t2", "title": "b", "lane": "eng"}])
    op = {"op": "task_update", "id": "move-2", "by": MASTER, "item": "tasks/t2", "fields": {"phase": "p2"}}
    state, rejected = core.sync(SLUG, ops=[op])
    assert rejected == [] and state["_meta"]["events"][-1]["text"] == "no phase to p2"


def test_an_engineer_cannot_move_a_task():
    state, rejected = move(ENGINEER, "p2")
    assert rejected == ["move-1"] and phase_of(state) == "p1"
    assert "cannot set a task phase" in state["_meta"]["warnings"][0]


def test_a_move_to_an_unknown_phase_is_refused():
    state, rejected = move(MASTER, "p9")
    assert rejected == ["move-1"] and phase_of(state) == "p1"
    assert "phase p9 is not on this ledger" in state["_meta"]["warnings"][0]
