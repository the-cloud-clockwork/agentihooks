import json
from types import SimpleNamespace

import pytest

from scripts.swarm_ledger import ledger, ledger_tasks, new_ledger
from scripts.swarm_ledger import ledger_core as core
from tests.swarm_ledger import legacy_page  # noqa: E402

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
    html_path.write_text(legacy_page.render(new_ledger.build_doc(content), SLUG, 8765), encoding="utf-8")
    core.sync(SLUG)
    for n, (lane, state, by) in enumerate(claims):
        kind = {"kind": "plan"} if lane == "plan" else {}
        task = {"op": "task_add", "id": f"seed-{n}", "by": "swarm", "task": f"c{n}", "title": "c", "phase": "p1"}
        fields = {"state": state, "claimed_by": by}
        claim = {"op": "task_update", "id": f"claim-{n}", "by": "swarm", "item": f"tasks/c{n}", "fields": fields}
        core.sync(SLUG, ops=[{**task, "lane": lane, **kind}, claim])


def add(by, phase="p1"):
    op = {"op": "task_add", "id": "add-t9", "by": by, "task": "t9", "title": "x", "lane": "eng"}
    return core.sync(SLUG, ops=[{**op, "phase": phase} if phase else op])


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


def append_phase(by):
    op = {"op": "phase_append", "id": "append-p9", "by": by, "phases": [{"phase": "p9", "title": "nine"}]}
    return core.sync(SLUG, ops=[op])[0]


def test_an_appended_phase_records_who_appended_it():
    make_ledger()
    phases = {p["id"]: p for p in append_phase(PLANNER)["phases"]}
    assert phases["p9"]["added_by"] == PLANNER
    assert "added_by" not in phases["p1"]


def test_a_planner_without_a_plan_task_adds_tasks_to_a_phase_it_appended():
    make_ledger()
    append_phase(PLANNER)
    assert_added(PLANNER, "p9")


def test_a_task_added_to_an_appended_phase_carries_its_published_plan_link():
    make_ledger()
    append_phase(PLANNER)
    link = {"plan_url": "https://example.com/plan/1"}
    core.sync(SLUG, ops=[{"op": "phase_update", "id": "pub-p9", "by": PLANNER, "item": "phases/p9", "fields": link}])
    state, _ = add(PLANNER, "p9")
    assert next(t for t in state["tasks"] if t["id"] == "t9")["plan_url"] == "https://example.com/plan/1"


def test_a_planner_without_a_plan_task_cannot_add_to_a_phase_another_appended():
    make_ledger()
    append_phase("master@abcdef-0001")
    assert_refused(PLANNER, f"{PLANNER} holds no plan task and cannot add tasks: {FOLLOWUP}", "p9")


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


@pytest.mark.parametrize("phase", ["p2", None])
def test_a_planner_cannot_add_a_task_outside_the_phase_it_plans(phase):
    make_ledger([plan_task()])
    assert_refused(PLANNER, f"{PLANNER} plans phase p1 and cannot add a task outside it: {FOLLOWUP}", phase)


@pytest.mark.parametrize("claims", [[], [plan_task("blocked")], [plan_task(by="planner@abcdef-0004")]])
def test_a_planner_holding_no_plan_task_cannot_add_a_task(claims):
    make_ledger(claims)
    assert_refused(PLANNER, f"{PLANNER} holds no plan task and cannot add tasks: {FOLLOWUP}")


def test_a_planner_engineer_task_in_its_phase_does_not_count_as_planning():
    make_ledger([("eng", "claimed", PLANNER)])
    assert_refused(PLANNER, f"{PLANNER} holds no plan task and cannot add tasks: {FOLLOWUP}")


def test_the_refusal_is_empty_for_an_author_who_may_add():
    assert ledger_tasks.add_refusal([], {"by": "master@abcdef-0001", "phase": "p1"}, set()) == ""


def test_task_add_prints_the_server_refusal(monkeypatch):
    args = SimpleNamespace(slug=SLUG, name="engineer@abcdef-0001")
    monkeypatch.setattr(
        ledger, "call", lambda slug, ops: {"rejected": ["x"], "_meta": {"warnings": ["refused because"]}}
    )
    with pytest.raises(SystemExit) as raised:
        ledger.send(args, "task_add", task="t9")
    assert raised.value.code == "refused because"


TAKEN = 'task c0 already exists as "c" (done): pick another id, or pass - as the id to mint one'


def served(slug, ops=None):
    state, rejected = core.sync(slug, ops=ops)
    return {**state, "rejected": rejected}


def cli(monkeypatch, *argv):
    monkeypatch.setattr(ledger, "call", served)
    monkeypatch.setattr(ledger, "resource", lambda slug, path, collection=False: served(slug)[path])
    ledger.cmd_task(ledger.build_parser().parse_args(["--slug", SLUG, "--as", "master@abcdef-0001", "task", *argv]))


def test_task_add_refuses_an_id_a_finished_task_holds_and_keeps_the_ledger():
    make_ledger([("eng", "done", "engineer@abcdef-0001")])
    before = core.sync(SLUG)[0]["tasks"]
    op = {"op": "task_add", "id": "add-c0", "by": "swarm", "task": "c0", "title": "new", "lane": "eng", "phase": "p1"}
    state, rejected = core.sync(SLUG, ops=[op])
    assert rejected == ["add-c0"]
    assert state["_meta"]["warnings"] == [TAKEN]
    assert state["tasks"] == before


def test_task_add_cli_exits_with_the_refusal_for_a_taken_id(monkeypatch, capsys):
    make_ledger([("eng", "done", "engineer@abcdef-0001")])
    with pytest.raises(SystemExit) as raised:
        cli(monkeypatch, "add", "c0", "new", "work")
    assert raised.value.code == TAKEN
    assert capsys.readouterr().out == ""


@pytest.mark.parametrize(
    ("ids", "minted"), [([], "t1"), (["t1", "t3", "plan-p1", "t7x", "c9"], "t4"), (["t09", "t2"], "t10")]
)
def test_next_id_is_one_past_the_highest_t_number(ids, minted):
    assert ledger_tasks.next_id([{"id": task} for task in ids]) == minted


def test_task_add_with_a_dash_mints_the_next_id_and_prints_it(monkeypatch, capsys):
    make_ledger()
    core.sync(SLUG, ops=[{"op": "task_add", "id": "seed", "by": "swarm", "task": "t4", "title": "a", "lane": "eng"}])
    cli(monkeypatch, "add", "-", "build", "it", "--phase", "p1")
    assert json.loads(capsys.readouterr().out) == {"task": "t5", "added": "build it"}
    assert [(t["id"], t["title"]) for t in core.sync(SLUG)[0]["tasks"]] == [("t4", "a"), ("t5", "build it")]


def test_the_refusal_names_a_task_without_a_state_as_open():
    ctx = SimpleNamespace(refused=[])
    assert ledger_tasks._add({"tasks": [{"id": "t1", "title": "old"}]}, {"task": "t1", "by": "swarm"}, ctx) is False
    assert ctx.refused == ['task t1 already exists as "old" (open): pick another id, or pass - as the id to mint one']


def test_task_add_with_a_dash_mints_t1_on_a_ledger_without_tasks(monkeypatch, capsys):
    sent = []
    monkeypatch.setattr(ledger, "call", lambda slug, ops=None: sent.append(ops) or {})
    monkeypatch.setattr(ledger, "resource", lambda slug, path, collection=False: sent.append(None) or [])
    ledger.cmd_task(ledger.build_parser().parse_args(["--slug", SLUG, "--as", "swarm", "task", "add", "-", "first"]))
    assert [op["task"] for op in sent[1]] == ["t1"]


def test_task_help_says_a_dash_mints_the_id(capsys):
    with pytest.raises(SystemExit):
        ledger.build_parser().parse_args(["task", "--help"])
    assert "id task id; task add - mints the next free t<n> values" in " ".join(capsys.readouterr().out.split())
