from types import SimpleNamespace

import pytest

from scripts.swarm_ledger import ledger, ledger_tasks, new_ledger
from scripts.swarm_ledger import ledger_core as core

SLUG = "taskadd-2026-01-01"
PLANNER = "planner@abcdef-0003"
FOLLOWUP = 'propose the work with agentihooks ledger followup add "<plain words>" and the master decides'


@pytest.fixture(autouse=True)
def gate(tmp_path, monkeypatch):
    monkeypatch.setattr(core, "LEDGER_DIR", tmp_path)
    monkeypatch.setitem(core.EXTENSION_OPS, "task_add", ledger_tasks)
    monkeypatch.setitem(core.EXTENSION_OPS, "task_update", ledger_tasks)


def make_ledger(claims=()):
    content = {
        "title": "Demo",
        "overview": "o",
        "sources": [],
        "phases": [{"title": "one", "description": "d"}, {"title": "two", "description": "d"}],
    }
    html_path, _ = core.paths(SLUG)
    html_path.write_text(new_ledger.render(new_ledger.build_doc(content), SLUG, 8765), encoding="utf-8")
    core.sync(SLUG)
    for n, (lane, state, by) in enumerate(claims):
        kind = {"kind": "plan"} if lane == "plan" else {}
        task = {"op": "task_add", "id": f"seed-{n}", "by": "swarm", "task": f"c{n}", "title": "c", "phase": "p1"}
        fields = {"state": state, "claimed_by": by}
        claim = {"op": "task_update", "id": f"claim-{n}", "by": "swarm", "item": f"tasks/c{n}", "fields": fields}
        core.sync(SLUG, ops=[{**task, "lane": lane, **kind}, claim])


def add(by, phase="p1"):
    op = {"op": "task_add", "id": "add-t9", "by": by, "task": "t9", "title": "x", "phase": phase, "lane": "eng"}
    return core.sync(SLUG, ops=[op])


def plan_task(state="claimed", by=PLANNER):
    return ("plan", state, by)


def assert_refused(by, reason, phase="p1"):
    state, rejected = add(by, phase)
    assert rejected == ["add-t9"]
    assert state["_meta"]["warnings"] == [reason]
    assert "t9" not in [t["id"] for t in state["tasks"]]


def assert_added(by, phase="p1"):
    state, rejected = add(by, phase)
    assert rejected == []
    assert "t9" in [t["id"] for t in state["tasks"]]


def test_an_engineer_cannot_add_a_task():
    make_ledger()
    assert_refused(
        "engineer@abcdef-0001", f"engineer@abcdef-0001 works in the eng lane and cannot add tasks: {FOLLOWUP}"
    )


def test_a_ci_agent_cannot_add_a_task():
    make_ledger()
    assert_refused("ci@abcdef-0002", f"ci@abcdef-0002 works in the ci lane and cannot add tasks: {FOLLOWUP}")


def test_a_legacy_engineer_name_cannot_add_a_task():
    make_ledger()
    assert_refused("sw-eng-1", f"sw-eng-1 works in the eng lane and cannot add tasks: {FOLLOWUP}")


@pytest.mark.parametrize("by", ["master@abcdef-0001", "operator", "swarm", "doctor", "sw-master-1"])
def test_master_operator_tick_and_doctor_add_tasks(by):
    make_ledger()
    assert_added(by)


def test_a_planner_adds_tasks_in_the_phase_it_plans():
    make_ledger([plan_task()])
    assert_added(PLANNER)


def test_a_planner_cannot_add_a_task_outside_the_phase_it_plans():
    make_ledger([plan_task()])
    assert_refused(PLANNER, f"{PLANNER} plans phase p1 and cannot add a task to phase p2: {FOLLOWUP}", "p2")


@pytest.mark.parametrize("claims", [[], [plan_task("blocked")], [plan_task(by="planner@abcdef-0004")]])
def test_a_planner_holding_no_plan_task_cannot_add_a_task(claims):
    make_ledger(claims)
    assert_refused(PLANNER, f"{PLANNER} holds no plan task and cannot add tasks: {FOLLOWUP}")


def test_a_planner_engineer_task_in_its_phase_does_not_count_as_planning():
    make_ledger([("eng", "claimed", PLANNER)])
    assert_refused(PLANNER, f"{PLANNER} holds no plan task and cannot add tasks: {FOLLOWUP}")


def test_the_refusal_is_empty_for_an_author_who_may_add():
    assert ledger_tasks.add_refusal([], {"by": "master@abcdef-0001", "phase": "p1"}) == ""


def test_task_add_prints_the_server_refusal(monkeypatch):
    args = SimpleNamespace(slug=SLUG, name="engineer@abcdef-0001")
    monkeypatch.setattr(
        ledger, "call", lambda slug, ops: {"rejected": ["x"], "_meta": {"warnings": ["refused because"]}}
    )
    with pytest.raises(SystemExit) as raised:
        ledger.send(args, "task_add", task="t9")
    assert raised.value.code == "refused because"
