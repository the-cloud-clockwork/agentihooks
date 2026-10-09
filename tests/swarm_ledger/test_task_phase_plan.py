import pytest

from scripts.swarm_ledger import ledger_core as core
from scripts.swarm_ledger import ledger_tasks, legacy_page, new_ledger
from tests.swarm_ledger.plan_slices import anchored

SLUG = "phase-plan"
MASTER = "master@323133-1"


@pytest.fixture(autouse=True)
def ledger_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(core, "LEDGER_DIR", tmp_path)
    monkeypatch.setitem(core.EXTENSION_OPS, "task_add", ledger_tasks)
    monkeypatch.setitem(core.EXTENSION_OPS, "task_update", ledger_tasks)
    content = {
        "title": "Demo",
        "overview": "o",
        "sources": [],
        "phases": [{"title": "one", "description": "d"}, {"title": "two", "description": "e"}],
    }
    html_path, _ = core.paths(SLUG)
    html_path.write_text(legacy_page.render(new_ledger.build_doc(content), SLUG, 8765), encoding="utf-8")
    state = anchored(SLUG, "old", "shared")
    url = state["phases"][0]["plan_ref"]["artifact"]
    phase_plan("p1", url)
    op = {
        "op": "task_add",
        "id": "seed",
        "by": MASTER,
        "task": "t1",
        "title": "Build feature",
        "lane": "eng",
        "phase": "p1",
        "plan_slice": "old",
    }
    core.check_op(op)
    assert core.sync(SLUG, ops=[op])[1] == []


def phase_plan(phase, url):
    op = {
        "op": "phase_update",
        "id": f"link-{phase}",
        "by": MASTER,
        "item": f"phases/{phase}",
        "fields": {"plan_url": url},
    }
    core.check_op(op)
    assert core.sync(SLUG, ops=[op])[1] == []


def update(**fields):
    op = {"op": "task_update", "id": f"move-{'-'.join(fields)}", "by": MASTER, "item": "tasks/t1", "fields": fields}
    core.check_op(op)
    state, rejected = core.sync(SLUG, ops=[op])
    return state, rejected


def task(state):
    return next(t for t in state["tasks"] if t["id"] == "t1")


@pytest.mark.parametrize("destination_plan", [False, True])
def test_moving_phase_clears_old_slice_lines_and_inherited_link(destination_plan):
    if destination_plan:
        phase_plan("p2", "https://example.com/new-plan")
    state, rejected = update(phase="p2")
    assert rejected == []
    moved = task(state)
    assert moved["phase"] == "p2"
    assert moved.get("plan_slice", "") == ""
    assert moved.get("plan_lines", "") == ""
    assert moved.get("plan_url", "") == ""
    assert state["_meta"]["stamps"]["tasks/t1/plan_lines"]["by"] == MASTER


def test_moving_phase_preserves_an_independent_task_plan_link():
    assert update(plan_url="https://example.com/task-plan")[1] == []
    state, rejected = update(phase="p2")
    assert rejected == []
    assert task(state)["plan_url"] == "https://example.com/task-plan"
    assert task(state).get("plan_slice", "") == ""
    assert task(state).get("plan_lines", "") == ""


def test_moving_phase_preserves_an_explicit_replacement_plan_link():
    state, rejected = update(phase="p2", plan_url="https://example.com/replacement")
    assert rejected == []
    assert task(state)["plan_url"] == "https://example.com/replacement"
    assert task(state).get("plan_slice", "") == ""
    assert task(state).get("plan_lines", "") == ""


@pytest.mark.parametrize("slice_name", ["new", "shared"])
def test_moving_phase_recomputes_supplied_slice_from_the_new_phase_plan(slice_name):
    state = anchored(SLUG, "new", "shared", phase="p2")
    url = state["phases"][1]["plan_ref"]["artifact"]
    phase_plan("p2", url)
    state, rejected = update(phase="p2", plan_slice=slice_name)
    assert rejected == []
    moved = task(state)
    assert moved["phase"] == "p2"
    assert moved["plan_slice"] == slice_name
    assert moved["plan_lines"] == ("2-3" if slice_name == "new" else "4-5")
    assert moved["plan_url"] == url


def test_setting_the_same_phase_keeps_the_existing_plan_metadata():
    before = task(core.sync(SLUG)[0]).copy()
    state, rejected = update(phase="p1")
    assert rejected == []
    assert task(state) == before


def test_a_refused_move_keeps_the_existing_plan_metadata():
    anchored(SLUG, "new", phase="p2")
    before = task(core.sync(SLUG)[0]).copy()
    state, rejected = update(phase="p2")
    assert rejected == ["move-phase"]
    assert task(state) == before
