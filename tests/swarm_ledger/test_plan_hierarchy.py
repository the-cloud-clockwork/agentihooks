import itertools

import pytest

from scripts.swarm_ledger import ledger_artifacts, ledger_plans, ledger_tasks, new_ledger
from scripts.swarm_ledger import ledger_core as core
from scripts.swarm_ledger.repository import rows
from tests.swarm_ledger import legacy_page

SLUG = "plan-hierarchy"
IDS = itertools.count()
PLAN = "# Plan\n\n## Build\n<!-- slice: first -->\n### First\nOne\n<!-- slice: second -->\n### Second\nTwo\n"


@pytest.fixture(autouse=True)
def hierarchy_ledger(tmp_path, monkeypatch):
    monkeypatch.setattr(core, "LEDGER_DIR", tmp_path)
    for name in ledger_tasks.OPS:
        monkeypatch.setitem(core.EXTENSION_OPS, name, ledger_tasks)
    content = {
        "title": "Hierarchy",
        "overview": "Intent",
        "sources": [],
        "phases": [{"title": "Build"}, {"title": "Ship"}],
    }
    html, json_path = core.paths(SLUG)
    json_path.unlink(missing_ok=True)
    html.write_text(legacy_page.render(new_ledger.build_doc(content), SLUG, 8765))
    core.sync(SLUG)


def run(kind, **fields):
    op = {"op": kind, "id": f"{kind}-{next(IDS)}", "by": "planner", **fields}
    core.check_op(op)
    state, rejected = core.sync(SLUG, ops=[op])
    return state, rejected, [alert["text"] for alert in state["alerts"]]


def plans(*ids):
    for plan in ids:
        assert run("plan_add", plan=plan, title=f"Plan {plan}")[1] == []


def link(phase, plan):
    return run("phase_update", item=f"phases/{phase}", fields={"plan": plan})


def test_plans_and_slices_are_ledger_collections():
    assert {"plans", "slices"} <= rows.COLLECTIONS
    plans("a")
    assert link("p1", "plans/a")[1] == []
    state, rejected, _ = run("slice_add", phase="phases/p1", anchor="first")
    assert rejected == []
    assert state["plans"] == [{"id": "a", "title": "Plan a", "artifact": "", "url": ""}]
    assert state["slices"] == [{"id": "a.first", "phase": "phases/p1", "anchor": "first", "lines": ""}]
    assert state["phases"][0]["plan"] == "plans/a"
    assert run("task_add", task="t1", title="Build it", lane="eng", phase="p1", slice="slices/a.first")[1] == []
    assert core.sync(SLUG)[0]["tasks"][0]["slice"] == "slices/a.first"


def test_a_slice_computes_its_lines_from_the_stored_plan():
    file = ledger_artifacts.store(SLUG, "plan.md", PLAN.encode())
    artifact = f"http://127.0.0.1:8765/artifacts/{SLUG}/{file['id']}"
    run("join", role="member")
    run("artifact_add", task="", title="Plan", file=file, plan=True)
    assert run("plan_add", plan="a", title="Plan", artifact=artifact)[1] == []
    link("p1", "plans/a")
    state, rejected, _ = run("slice_add", phase="phases/p1", anchor="second")
    assert rejected == []
    assert state["slices"][0]["lines"] == "7-9"
    _, rejected, refusal = run("slice_add", phase="phases/p1", anchor="missing")
    assert rejected and "slice anchor missing is missing or repeated in its phase" in refusal


@pytest.mark.parametrize(
    ("kind", "fields", "refusal"),
    [
        ("phase", "plans/missing", "phase p1 names an unknown plan plans/missing"),
        ("slice", "phases/missing", "slice first names an unknown phase phases/missing"),
        ("task", "slices/a.missing", "task t1 names an unknown slice slices/a.missing"),
    ],
)
def test_an_unknown_parent_is_refused(kind, fields, refusal):
    plans("a")
    link("p1", "plans/a")
    outcome = {
        "phase": lambda: link("p1", fields),
        "slice": lambda: run("slice_add", phase=fields, anchor="first"),
        "task": lambda: run("task_add", task="t1", title="Build", lane="eng", phase="p1", slice=fields),
    }[kind]()
    assert outcome[1] and refusal in outcome[2]


def test_a_parent_on_another_ledger_is_refused():
    plans("a")
    _, rejected, refusal = link("p1", "ledgers/other/plans/a")
    assert rejected
    assert (
        "phase p1 names ledgers/other/plans/a on ledger other: a parent lives on the same ledger, named as plans/<id>"
    ) in refusal


@pytest.mark.parametrize(
    ("kind", "parent", "refusal"),
    [
        ("phase", "slices/a.first", "phase p1 needs a plan as its parent, not slices/a.first"),
        ("slice", "plans/a", "slice second needs a phase as its parent, not plans/a"),
        ("task", "plans/a", "task t1 needs a slice as its parent, not plans/a"),
        ("task", "a.first", "task t1 needs a slice as its parent, not a.first"),
    ],
)
def test_a_parent_of_the_wrong_kind_is_refused(kind, parent, refusal):
    plans("a")
    link("p1", "plans/a")
    run("slice_add", phase="phases/p1", anchor="first")
    outcome = {
        "phase": lambda: link("p1", parent),
        "slice": lambda: run("slice_add", phase=parent, anchor="second"),
        "task": lambda: run("task_add", task="t1", title="Build", lane="eng", phase="p1", slice=parent),
    }[kind]()
    assert outcome[1] and refusal in outcome[2]


def test_a_task_whose_slice_belongs_to_another_phase_is_refused():
    plans("a")
    link("p1", "plans/a")
    link("p2", "plans/a")
    run("slice_add", phase="phases/p1", anchor="first")
    refusal = "task t1 is in phase p2 but its slice slices/a.first belongs to phases/p1"
    _, rejected, text = run("task_add", task="t1", title="Build", lane="eng", phase="p2", slice="slices/a.first")
    assert rejected and refusal in text
    run("task_add", task="t1", title="Build", lane="eng", phase="p2")
    _, rejected, text = run("task_update", item="tasks/t1", fields={"slice": "slices/a.first"})
    assert rejected and refusal in text


def test_moving_a_task_to_another_phase_drops_its_slice():
    plans("a")
    link("p1", "plans/a")
    run("slice_add", phase="phases/p1", anchor="first")
    run("task_add", task="t1", title="Build", lane="eng", phase="p1", slice="slices/a.first")
    state, rejected, _ = run("task_update", item="tasks/t1", fields={"phase": "p2"})
    assert rejected == []
    assert state["tasks"][0]["phase"] == "p2"
    assert state["tasks"][0]["slice"] == ""


def test_the_same_anchor_in_two_plans_stays_two_slices():
    plans("a", "b")
    link("p1", "plans/a")
    link("p2", "plans/b")
    run("slice_add", phase="phases/p1", anchor="first")
    state, rejected, _ = run("slice_add", phase="phases/p2", anchor="first")
    assert rejected == []
    assert [(s["id"], s["phase"]) for s in state["slices"]] == [("a.first", "phases/p1"), ("b.first", "phases/p2")]
    assert run("slice_add", phase="phases/p2", anchor="first")[1] == []
    assert len(core.sync(SLUG)[0]["slices"]) == 2


def test_the_same_anchor_twice_in_one_plan_is_refused():
    plans("a")
    link("p1", "plans/a")
    link("p2", "plans/a")
    run("slice_add", phase="phases/p1", anchor="first")
    _, rejected, refusal = run("slice_add", phase="phases/p2", anchor="first")
    assert rejected and "slice a.first already belongs to phases/p1" in refusal


def test_a_phase_holding_slices_keeps_its_plan():
    plans("a", "b")
    link("p1", "plans/a")
    run("slice_add", phase="phases/p1", anchor="first")
    _, rejected, refusal = link("p1", "plans/b")
    assert rejected and "phase p1 holds slices of another plan: a.first" in refusal
    assert link("p1", "plans/a")[1] == []


def test_a_slice_needs_a_phase_with_a_plan():
    _, rejected, refusal = run("slice_add", phase="phases/p1", anchor="first")
    assert rejected and "phase p1 has no plan: link it to a plan before adding a slice" in refusal


def test_a_plan_id_is_taken_once():
    plans("a")
    _, rejected, refusal = run("plan_add", plan="a", title="Other")
    assert rejected and "plan a already exists" in refusal


def test_legacy_plan_fields_are_checked_against_the_new_parents():
    plans("a")
    link("p1", "plans/a")
    run("slice_add", phase="phases/p1", anchor="first")
    _, rejected, refusal = run(
        "task_add", task="t1", title="Build", lane="eng", phase="p1", slice="slices/a.first", plan_slice="second"
    )
    assert rejected and "task t1 plan_slice second differs from its slice anchor first" in refusal
    assert run("plan_add", plan="b", title="Plan b", url="https://github.com/acme/app/issues/2")[1] == []
    _, rejected, refusal = run(
        "phase_update",
        item="phases/p2",
        fields={"plan": "plans/b", "plan_url": "https://github.com/acme/app/issues/3"},
    )
    assert rejected and "phase p2 plan_url https://github.com/acme/app/issues/3 is not a link of plans/b" in refusal


@pytest.mark.parametrize(
    ("op", "error"),
    [
        ({"op": "slice_add", "phase": "phases/p1"}, "slice_add needs phase and an anchor"),
        ({"op": "slice_add", "phase": "phases/p1", "anchor": 5}, "slice_add needs phase and an anchor"),
        ({"op": "plan_add", "title": "A"}, "plan_add needs plan, an id of letters, digits, _ and -"),
        (
            {"op": "plan_add", "plan": "a.b", "title": "Dotted"},
            "plan_add needs plan, an id of letters, digits, _ and -",
        ),
        ({"op": "plan_add", "plan": "a", "title": " "}, "plan_add needs a title"),
        ({"op": "plan_add", "plan": "a", "title": "A", "url": "ftp://x"}, "url must be an http or https link"),
        ({"op": "plan_add", "plan": "a", "title": "A", "artifact": 1}, "artifact must be an http or https link"),
        ({"op": "slice_add", "phase": "phases/p1", "anchor": "bad anchor"}, "slice_add needs phase and an anchor"),
        ({"op": "slice_add", "phase": 3, "anchor": "first"}, "slice_add needs phase and an anchor"),
        ({"op": "plan_add", "plan": "a", "title": "A", "extra": 1}, "plan_add takes only plan title artifact url"),
    ],
)
def test_malformed_plan_ops_are_refused(op, error):
    with pytest.raises(ValueError, match=f"^{error}$"):
        ledger_plans.check({"id": "x", "by": "planner", **op})


@pytest.mark.parametrize(
    ("doc", "error"),
    [
        ({"phases": [{"id": "p1", "plan": "plans/a"}]}, "phase p1 names an unknown plan plans/a"),
        ({"plans": [{"id": "a"}], "phases": [{"id": "p1", "plan": 3}]}, "phases/p1/plan must be str"),
        ({"plans": [{"id": "a"}, {"id": "a"}]}, "every plans item needs a unique id"),
        ({"slices": [{"id": "a.x", "phase": "phases/p1", "anchor": "x"}]}, "slice x names an unknown phase phases/p1"),
        ({"tasks": [{"id": "t1", "phase": "p1", "slice": "slices/a.x"}]}, "task t1 names an unknown slice slices/a.x"),
    ],
)
def test_a_seeded_document_is_validated(doc, error):
    with pytest.raises(ValueError, match=f"^{error}$"):
        ledger_plans.validate({"plans": [], "slices": [], "phases": [], "tasks": [], **doc})


def test_a_new_document_carries_empty_plan_collections():
    doc = core.normalize({"title": "x"})
    assert doc["plans"] == [] and doc["slices"] == []
    with pytest.raises(ValueError, match="^phase p1 names an unknown plan plans/a$"):
        core.validate({**doc, "phases": [{"id": "p1", "title": "P", "plan": "plans/a"}]})
