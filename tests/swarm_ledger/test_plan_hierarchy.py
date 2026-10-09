import copy
import itertools

import pytest

from scripts.swarm_ledger import ledger_artifacts, ledger_phases, ledger_plans, ledger_tasks, new_ledger
from scripts.swarm_ledger import ledger_core as core
from scripts.swarm_ledger.api import schemas
from scripts.swarm_ledger.repository import rows
from tests.swarm_ledger import legacy_page
from tests.swarm_ledger.plan_slices import anchored

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


def test_a_phase_moved_to_another_plan_drops_slices_whose_anchor_the_new_plan_lacks():
    plans("a", "b")
    link("p1", "plans/a")
    run("slice_add", phase="phases/p1", anchor="first")
    run("task_add", task="t1", title="Build", lane="eng", phase="p1", slice="slices/a.first")
    state, rejected, _ = link("p1", "plans/b")
    assert rejected == []
    assert (state["phases"][0]["plan"], state["slices"]) == ("plans/b", [])
    assert "slice" not in state["tasks"][0]
    events = [(e["kind"], e["target"]) for e in state["_meta"]["events"] if e["kind"].startswith("slice")]
    assert events == [("slice cleared", "tasks/t1")]


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
    task = {"id": "t1", "phase": "p1", "slice": "slices/a.first", "plan_slice": "second"}
    refusal = ledger_plans.task_refusal(core.sync(SLUG)[0], task)
    assert refusal == "task t1 plan_slice second differs from its slice anchor first"
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
        ({"op": "slice_add", "phase": "phases/p1", "anchor": "x", "extra": 1}, "slice_add takes only phase anchor"),
        ({"op": "plan_add", "by": 5, "plan": "a", "title": "A"}, "plan_add needs by, an agent name or operator"),
        (
            {"op": "slice_add", "by": "", "phase": "phases/p1", "anchor": "x"},
            "slice_add needs by, an agent name or operator",
        ),
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
        ({"plans": [1]}, "plans must be a list of objects"),
        ({"plans": [{"id": ""}]}, "every plans item needs a unique id"),
        ({"plans": [{"id": 5}]}, "every plans item needs a unique id"),
        ({"slices": 3}, "slices must be a list of objects"),
        ({"slices": [{"id": "a.x"}, {"id": "a.x"}]}, "every slices item needs a unique id"),
        ({"plans": [{"id": "a", "url": "ftp://x"}]}, "url must be an http or https link"),
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


def test_a_task_naming_its_slice_needs_no_plan_slice_in_an_anchored_phase():
    file = ledger_artifacts.store(SLUG, "plan.md", PLAN.encode())
    artifact = f"http://127.0.0.1:8765/artifacts/{SLUG}/{file['id']}"
    run("join", role="member")
    run("artifact_add", task="", title="Plan", file=file, plan=True)
    run("plan_add", plan="a", title="Plan", artifact=artifact)
    assert run("phase_update", item="phases/p1", fields={"plan": "plans/a", "plan_url": artifact})[1] == []
    assert run("slice_add", phase="phases/p1", anchor="first")[1] == []
    _, rejected, refusal = run("task_add", task="t0", title="Build", lane="eng", phase="p1")
    assert rejected and any(text.startswith("phase p1 has a plan with slice anchors") for text in refusal)
    state, rejected, _ = run("task_add", task="t1", title="Build", lane="eng", phase="p1", slice="slices/a.first")
    assert rejected == []
    assert (state["tasks"][-1]["plan_slice"], state["tasks"][-1]["plan_lines"]) == ("first", "4-6")
    assert ledger_tasks.unsliced_refusal(state, {"phase": "p1", "slice": "slices/a.first"}, "planner") == ""
    run("task_add", task="t2", title="Build", lane="eng", phase="p1", plan_slice="first")
    _, rejected, refusal = run("task_update", item="tasks/t2", fields={"slice": "slices/a.second"})
    assert rejected and "task t2 names an unknown slice slices/a.second" in refusal
    run("slice_add", phase="phases/p1", anchor="second")
    state, rejected, _ = run("task_update", item="tasks/t2", fields={"slice": "slices/a.second"})
    assert rejected == []
    assert (state["tasks"][-1]["plan_slice"], state["tasks"][-1]["plan_lines"]) == ("second", "7-9")


def test_legacy_lines_and_plan_ref_are_checked_against_the_new_parents():
    doc = {
        "plans": [{"id": "a", "artifact": "http://host/artifacts/x/1", "url": ""}],
        "slices": [{"id": "a.first", "phase": "phases/p1", "anchor": "first", "lines": "4-6"}],
        "phases": [{"id": "p1", "plan": "plans/a"}],
    }
    task = {"id": "t1", "phase": "p1", "slice": "slices/a.first", "plan_lines": "4-5"}
    assert ledger_plans.task_refusal(doc, task) == "task t1 plan_lines 4-5 differ from its slice lines 4-6"
    assert ledger_plans.task_refusal(doc, {**task, "plan_lines": "4-6"}) == ""
    phase = {"id": "p1", "plan": "plans/a", "plan_ref": {"artifact": "http://host/artifacts/x/2", "lines": "1-9"}}
    assert (
        ledger_plans.phase_refusal(doc, phase) == "phase p1 plan_ref http://host/artifacts/x/2 is not a link of plans/a"
    )
    assert ledger_plans.phase_refusal(doc, {**phase, "plan_ref": {"artifact": "http://host/artifacts/x/1"}}) == ""


class Recorder:
    def __init__(self):
        self.refused, self.events = [], []

    def record(self, by, kind, target, **extra):
        self.events.append((by, kind, target, extra))


def test_plan_and_slice_ops_start_their_collections_and_record_events():
    doc, ctx = {"phases": [{"id": "p1", "plan": "plans/a"}]}, Recorder()
    assert ledger_plans.apply(doc, {"op": "plan_add", "by": "planner", "plan": "a", "title": " Plan "}, ctx)
    assert ledger_plans.apply(doc, {"op": "plan_add", "by": "planner", "plan": "a", "title": "Plan"}, ctx)
    assert ledger_plans.apply(doc, {"op": "slice_add", "by": "planner", "phase": "phases/p1", "anchor": "x"}, ctx)
    assert ctx.events == [
        ("planner", "added", "plans/a", {"text": "Plan"}),
        ("planner", "added", "slices/a.x", {"text": "x"}),
    ]
    assert doc["slices"] == [{"id": "a.x", "phase": "phases/p1", "anchor": "x", "lines": ""}]
    assert ctx.refused == []


def test_a_document_without_plan_collections_validates():
    assert ledger_plans.validate({}) is None
    assert ledger_plans.validate({"phases": [{"id": "p1"}], "tasks": [{"id": "t1"}]}) is None
    with pytest.raises(ValueError, match="^phase p1 names an unknown plan plans/a$"):
        ledger_plans.validate({"phases": [{"id": "p1", "plan": "plans/a"}]})
    assert ledger_plans.phase_refusal({"plans": [{"id": "a"}]}, {"id": "p1", "plan": "plans/a"}) == ""
    assert ledger_plans.with_plan_slice({}, {"slice": "slices/a.x"}) == {"slice": "slices/a.x"}


def test_phase_refusals_name_every_stale_slice_and_check_only_present_links():
    doc = {
        "plans": [{"id": "a"}, {"id": "b", "artifact": "http://host/1", "url": ""}],
        "slices": [{"id": "a.x", "phase": "phases/p1"}, {"id": "a.y", "phase": "phases/p1"}],
    }
    assert ledger_plans.phase_refusal(doc, {"id": "p1", "plan": "plans/b"}) == (
        "phase p1 holds slices of another plan: a.x, a.y"
    )
    assert ledger_plans.phase_refusal(doc, {"id": "p2", "plan": "plans/a", "plan_url": "http://any"}) == ""
    assert ledger_plans.phase_refusal(doc, {"id": "p2", "plan": "plans/b", "plan_url": "http://host/1"}) == ""
    assert ledger_plans.phase_refusal(doc, {"id": "p2", "plan": "plans/b", "plan_url": "http://host/2"}) == (
        "phase p2 plan_url http://host/2 is not a link of plans/b"
    )


def test_a_task_without_a_phase_is_refused_its_slice():
    doc = {"slices": [{"id": "a.x", "phase": "phases/p1", "anchor": "x"}]}
    assert ledger_plans.task_refusal(doc, {"id": "t1", "slice": "slices/a.x"}) == (
        "task t1 is in phase none but its slice slices/a.x belongs to phases/p1"
    )


def test_only_an_update_of_a_parent_field_checks_the_slice_link():
    doc = {
        "slices": [{"id": "a.x", "phase": "phases/p1", "anchor": "x"}],
        "tasks": [{"id": "t1", "phase": "p2", "slice": "slices/a.x"}],
    }
    assert ledger_tasks._parent_refusal(doc, {"item": "tasks/t1", "fields": {"state": "done"}}) == ""
    assert ledger_tasks._parent_refusal(doc, {"item": "tasks/t1", "fields": {"phase": "p2"}}) == (
        "task t1 is in phase p2 but its slice slices/a.x belongs to phases/p1"
    )


def test_a_named_slice_keeps_a_plan_slice_already_given():
    doc = {"slices": [{"id": "a.x", "phase": "phases/p1", "anchor": "x", "lines": "2-3"}]}
    fields = {"slice": "slices/a.x", "plan_slice": "y"}
    assert ledger_plans.with_plan_slice(doc, fields) is fields
    assert ledger_plans.with_plan_slice(doc, {"slice": "slices/a.x"}) == {"slice": "slices/a.x", "plan_slice": "x"}


def test_a_slice_takes_its_lines_from_the_phase_plan_reference():
    anchored(SLUG, "build")
    plans("a")
    assert link("p1", "plans/a")[1] == []
    state, rejected, _ = run("slice_add", phase="phases/p1", anchor="build")
    assert rejected == []
    assert state["slices"][0]["lines"] == "2-3"


@pytest.mark.parametrize("key", ["phase", "description", "workspace", "slice"])
def test_task_text_fields_must_be_strings(key):
    op = {"op": "task_add", "id": "x", "by": "planner", "task": "t1", "title": "T", "lane": "eng", key: 3}
    with pytest.raises(ValueError, match="^phase, description, workspace and slice must be strings$"):
        ledger_tasks.check(op)


def test_a_phase_plan_must_be_a_string():
    op = {"op": "phase_update", "id": "x", "by": "planner", "item": "phases/p1", "fields": {"plan": 3}}
    with pytest.raises(ValueError, match="^plan must be a string$"):
        ledger_phases.check(op)


def test_appended_phases_are_checked_against_their_plans():
    plans("a")
    _, rejected, refusal = run("phase_append", phases=[{"phase": "p3", "title": "Third", "plan": "plans/b"}])
    assert rejected and "phase p3 names an unknown plan plans/b" in refusal
    state, rejected, _ = run("phase_append", phases=[{"phase": "p3", "title": "Third", "plan": "plans/a"}])
    assert rejected == []
    assert state["phases"][-1]["plan"] == "plans/a"


def test_plan_ops_target_their_collections_and_artifacts_keep_a_boolean_plan():
    assert schemas.target({"op": "plan_add"}) == "plans"
    assert schemas.target({"op": "slice_add"}) == "slices"
    assert schemas.operation_schema("artifact_add")["properties"]["plan"] == {"type": "boolean"}
    assert schemas.operation_schema("plan_add")["properties"]["plan"]["type"] == "string"


PLANNED = {
    "phases": [{"title": "No id"}, {"id": "p1", "plan": "plans/plan-a"}, {"id": "p2"}],
    "slices": [{"anchor": "x"}, {"id": "plan-a.first"}],
}


@pytest.mark.parametrize(
    "fields",
    [
        {"phase": "p1", "plan_slice": "first", "slice": "slices/plan-a.other"},
        {"phase": "p1", "plan_slice": ""},
        {"phase": "p1"},
        {"phase": "p2", "plan_slice": "first"},
        {"phase": "p1", "plan_slice": "missing"},
        {"phase": "p9", "plan_slice": "first"},
    ],
)
def test_with_slice_leaves_fields_without_a_planned_slice_alone(fields):
    assert ledger_plans.with_slice(PLANNED, fields) is fields


def test_with_slice_names_the_slice_of_the_phase_plan():
    fields = {"phase": "p1", "plan_slice": "first", "title": "Build"}
    assert ledger_plans.with_slice(PLANNED, fields) == {**fields, "slice": "slices/plan-a.first"}


@pytest.mark.parametrize("doc", [{}, {"phases": [{"id": "p1", "plan": "plans/plan-a"}]}])
def test_with_slice_reads_a_document_without_phases_or_slices(doc):
    fields = {"phase": "p1", "plan_slice": "first"}
    assert ledger_plans.with_slice(doc, fields) is fields


def test_plan_and_slice_ids_come_from_the_file_and_the_anchor():
    assert ledger_plans.plan_id("0123456789abcdef.md") == "plan-0123456789ab"
    assert ledger_plans.slice_id("plan-a", "first") == "plan-a.first"


@pytest.mark.parametrize(
    ("phases", "events", "kept", "left"),
    [
        ([{"id": "p1"}], [{"kind": "added", "target": "plans/b"}], ["a"], []),
        (
            [{"id": "p1", "plan": "plans/b"}],
            [{"kind": "added", "target": "plans/b"}],
            ["a", "b"],
            [{"kind": "added", "target": "plans/b"}],
        ),
        (
            [{"id": "p1"}],
            [{"kind": "changed", "target": "plans/b"}],
            ["a", "b"],
            [{"kind": "changed", "target": "plans/b"}],
        ),
        ([{"id": "p1"}], [], ["a", "b"], []),
    ],
)
def test_a_refused_update_drops_only_an_unnamed_plan_this_batch_added(phases, events, kept, left):
    doc = {"phases": phases, "plans": [{"id": "a"}, {"id": "b"}]}
    ctx = Recorder()
    ctx.events = [*events, {"kind": "added", "target": "tasks/t1"}]
    ledger_plans.drop_unused(doc, "plans/b", ctx)
    assert [row["id"] for row in doc["plans"]] == kept
    assert ctx.events == [*left, {"kind": "added", "target": "tasks/t1"}]


def test_a_batch_whose_phase_update_is_rejected_before_it_applies_keeps_no_plan_it_added():
    ops = [
        {"op": "plan_add", "id": "add-c", "by": "planner", "plan": "c", "title": "Plan c"},
        {"op": "phase_update", "id": "move-p9", "by": "planner", "item": "phases/p9", "fields": {"plan": "plans/c"}},
    ]
    state, rejected = core.sync(SLUG, ops=ops)
    assert rejected == ["add-c", "move-p9"]
    assert state["plans"] == []
    assert [e for e in state["_meta"]["events"] if e["target"] == "plans/c"] == []


def test_drop_refused_drops_only_plans_named_by_rejected_phase_updates():
    doc = {"phases": [{"id": "p1"}], "plans": [{"id": "a"}, {"id": "b"}, {"id": "c"}, {"id": "d"}]}
    ctx = Recorder()
    ctx.events = [{"kind": "added", "target": f"plans/{plan}"} for plan in ("a", "b", "c", "d")]
    ops = [
        {"op": "phase_update", "id": "u1", "fields": {"plan": "plans/a"}},
        {"op": "phase_update", "id": "u2", "fields": {"plan": "plans/b"}},
        {"op": "plan_add", "id": "u3", "plan": "c"},
        {"op": "phase_update", "id": "u4", "fields": {"title": "T"}},
        {"op": "phase_add", "id": "u5", "phase": "p2", "plan": "plans/d"},
    ]
    ledger_plans.drop_refused(doc, ops, ["u1", "u3", "u4", "u5"], ctx)
    assert [row["id"] for row in doc["plans"]] == ["b", "c"]
    assert ctx.events == [{"kind": "added", "target": "plans/b"}, {"kind": "added", "target": "plans/c"}]


def test_resliced_names_changed_and_cleared_tasks_only_among_those_that_held_a_slice():
    before = [{"id": "a", "slice": "slices/x.one"}, {"id": "b", "slice": "slices/x.two"}, {"id": "c"}]
    before.append({"id": "e", "slice": "slices/y.one"})
    after = [{"id": "a", "slice": "slices/y.one"}, {"id": "b"}, {"id": "c", "slice": "slices/y.one"}]
    after += [{"id": "e", "slice": "slices/y.one"}, {"id": "f", "slice": "slices/y.one"}]
    assert ledger_plans.resliced(before, after) == {"changed": ["a"], "cleared": ["b"]}


@pytest.mark.parametrize(
    "doc",
    [
        {"plans": [{"id": "a"}], "slices": [{"id": "a.x", "phase": "phases/p1", "anchor": "x"}]},
        {"plans": [{"id": "b"}], "slices": [{"id": "b.x", "phase": "phases/p1", "anchor": "x"}]},
        {"plans": [{"id": "a"}], "slices": [{"id": "b.x", "phase": "phases/p2", "anchor": "x"}]},
    ],
)
def test_a_phase_keeps_its_slices_unless_it_holds_slices_of_an_earlier_plan(doc):
    assert ledger_plans.moved(doc, {"id": "p1", "plan": "plans/a"}) == {}


def test_settle_records_each_task_whose_slice_changed_or_cleared():
    doc, ctx = (
        {"tasks": [{"id": "a", "slice": "slices/x.1"}, {"id": "b", "slice": "slices/x.2"}, {"id": "c"}]},
        Recorder(),
    )
    view = {"tasks": [{"id": "a", "slice": "slices/y.1"}, {"id": "b"}, {"id": "c"}], "slices": []}
    ledger_plans.settle(doc, view, "planner", ctx)
    assert (doc["tasks"], doc["slices"]) == (view["tasks"], [])
    assert ctx.events == [("planner", "slice changed", "tasks/a", {}), ("planner", "slice cleared", "tasks/b", {})]


ISSUE = "https://github.com/acme/app/issues/12"
REF = {"artifact": "http://127.0.0.1:8765/artifacts/s/f.md", "lines": "1-9"}


def held():
    plans("a", "b")
    link("p1", "plans/a")
    run("slice_add", phase="phases/p1", anchor="first")
    run("task_add", task="t1", title="Build", lane="eng", phase="p1", slice="slices/a.first")
    return core.sync(SLUG)[0]


def unchanged(before, after):
    keys = ("plans", "phases", "slices", "tasks")
    return [after[key] for key in keys] == [before[key] for key in keys]


def test_a_batch_that_adds_a_plan_commits_none_of_its_ops_when_one_is_refused():
    ops = [
        {"op": "plan_add", "id": "add-c", "by": "planner", "plan": "c", "title": "Plan c"},
        {"op": "phase_update", "id": "move-p1", "by": "planner", "item": "phases/p1", "fields": {"plan": "plans/c"}},
        {"op": "phase_update", "id": "move-p2", "by": "planner", "item": "phases/p2", "fields": {"plan": "plans/x"}},
    ]
    state, rejected = core.sync(SLUG, ops=ops)
    assert rejected == ["add-c", "move-p1", "move-p2"]
    assert (state["plans"], [phase.get("plan") for phase in state["phases"]]) == ([], [None, None])
    assert [e for e in state["_meta"]["events"] if e["target"] in ("plans/c", "phases/p1")] == []
    assert [alert["text"] for alert in state["alerts"]] == ["phase p2 names an unknown plan plans/x"]


def test_a_batch_without_a_plan_keeps_the_ops_that_were_accepted():
    plans("c")
    ops = [
        {"op": "phase_update", "id": "move-p1", "by": "planner", "item": "phases/p1", "fields": {"plan": "plans/c"}},
        {"op": "phase_update", "id": "move-p2", "by": "planner", "item": "phases/p2", "fields": {"plan": "plans/x"}},
    ]
    state, rejected = core.sync(SLUG, ops=ops)
    assert rejected == ["move-p2"]
    assert [phase.get("plan") for phase in state["phases"]] == ["plans/c", None]


STALE = "phase p1 moves to plans/b without a new plan_ref: publish the plan for the phase to move it"


@pytest.mark.parametrize("reference", [{"plan_url": ISSUE}, {"plan_ref": REF}])
def test_stale_refusal_names_a_plan_change_without_a_new_plan_ref(reference):
    phase = {"id": "p1", "plan": "plans/a", **reference}
    assert ledger_plans.stale_refusal(phase, {"plan": "plans/b"}) == STALE
    with pytest.raises(ValueError) as raised:
        ledger_plans.check_move(phase, {"plan": "plans/b"})
    assert str(raised.value) == STALE
    assert ledger_plans.check_move(phase, {"plan": "plans/a"}) is None


def test_a_plan_change_without_a_new_plan_url_link_is_refused_and_changes_nothing():
    held()
    run("phase_update", item="phases/p1", fields={"plan_url": ISSUE})
    before = core.sync(SLUG)[0]
    state, rejected, refusal = link("p1", "plans/b")
    assert (len(rejected), refusal) == (1, [STALE])
    assert unchanged(before, state)


def test_a_plan_change_without_a_new_plan_ref_is_refused_and_changes_nothing():
    file = ledger_artifacts.store(SLUG, "plan.md", PLAN.encode())
    artifact = f"http://127.0.0.1:8765/artifacts/{SLUG}/{file['id']}"
    run("join", role="member")
    run("artifact_add", task="", title="Plan", file=file, plan=True)
    run("plan_add", plan="a", title="Plan", artifact=artifact)
    plans("b")
    fields = {"plan": "plans/a", "plan_ref": {"artifact": artifact, "lines": "3-9"}}
    assert run("phase_update", item="phases/p1", fields=fields)[1] == []
    assert run("slice_add", phase="phases/p1", anchor="first")[1] == []
    before = core.sync(SLUG)[0]
    assert before["slices"] == [{"id": "a.first", "phase": "phases/p1", "anchor": "first", "lines": "4-6"}]
    state, rejected, refusal = link("p1", "plans/b")
    assert (len(rejected), refusal) == (1, [STALE])
    assert unchanged(before, state)


@pytest.mark.parametrize(
    ("phase", "fields"),
    [
        ({"id": "p1", "plan": "plans/a", "plan_ref": REF}, {"plan": "plans/b", "plan_ref": REF}),
        ({"id": "p1", "plan": "plans/a"}, {"plan": "plans/b"}),
        ({"id": "p1", "plan": "plans/a", "plan_url": ISSUE}, {"plan": "plans/a"}),
        ({"id": "p1", "plan": "plans/a", "plan_url": ISSUE}, {"title": "Build"}),
        ({"id": "p1", "plan_url": ISSUE}, {"plan": "plans/b"}),
        ({}, {"plan": "plans/b"}),
    ],
)
def test_a_new_plan_ref_a_kept_plan_or_a_first_plan_link_is_not_stale(phase, fields):
    assert ledger_plans.stale_refusal(phase, fields) == ""


def test_moving_a_phase_onto_a_plan_that_cannot_be_read_is_refused_and_keeps_its_slices():
    before = held()
    state, rejected, refusal = run("phase_update", item="phases/p1", fields={"plan": "plans/b", "plan_url": ISSUE})
    assert (len(rejected), refusal) == (
        1,
        ["phase p1 plan cannot be read: plan artifact must name a stored ledger artifact"],
    )
    assert unchanged(before, state)
    assert state["tasks"][0]["slice"] == "slices/a.first"


def test_moved_raises_for_an_unreadable_plan_and_leaves_the_document_alone():
    doc = {
        "plans": [{"id": "b"}],
        "slices": [{"id": "a.x", "phase": "phases/p1", "anchor": "x"}],
        "tasks": [{"id": "t1", "slice": "slices/a.x"}],
    }
    kept = copy.deepcopy(doc)
    with pytest.raises(ValueError) as raised:
        ledger_plans.moved(doc, {"id": "p1", "plan": "plans/b", "plan_url": ISSUE})
    assert str(raised.value) == "phase p1 plan cannot be read: plan artifact must name a stored ledger artifact"
    assert doc == kept
