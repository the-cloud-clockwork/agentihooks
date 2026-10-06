import pytest

from scripts.swarm_ledger import ledger_core as core
from scripts.swarm_ledger import ledger_kinds, new_ledger


@pytest.fixture
def plan_ledger():
    content = {"title": "Plan proof", "overview": "Project intent", "sources": [], "phases": [{"title": "Build"}]}
    html, json_path = core.paths("plan-proof")
    json_path.unlink(missing_ok=True)
    html.parent.mkdir(parents=True, exist_ok=True)
    html.write_text(new_ledger.render(new_ledger.build_doc(content), "plan-proof", 8765))
    core.sync("plan-proof")
    return "plan-proof"


def add(slug, task, **fields):
    op = {
        "op": "task_add",
        "id": f"add-{task}",
        "by": "planner",
        "task": task,
        "title": "Build a feature",
        "lane": "eng",
        "phase": "p1",
        **fields,
    }
    core.check_op(op)
    return core.sync(slug, ops=[op])


def update(slug, **fields):
    op = {"op": "task_update", "id": "finish-plan", "by": "planner", "item": "tasks/plan", "fields": fields}
    core.check_op(op)
    return core.sync(slug, ops=[op])


def test_plan_requires_slice_and_stores_valid_phase_tasks(plan_ledger):
    assert ledger_kinds.unmet({"kind": "plan"}) == ["slice"]
    add(plan_ledger, "plan", lane="plan", kind="plan")
    add(plan_ledger, "build")
    state, _ = update(plan_ledger, state="done")
    assert state["tasks"][0]["state"] == "open"
    state, _ = update(plan_ledger, state="done", proof={"slice": " build "})
    assert state["tasks"][0]["state"] == "done"
    assert state["tasks"][0]["proof"] == {"slice": " build "}


@pytest.mark.parametrize(("lane", "kind"), [("plan", "code"), ("eng", "plan"), ("ci", "plan")])
def test_plan_lane_accepts_only_plan_tasks(plan_ledger, lane, kind):
    with pytest.raises(ValueError, match="plan.*lane"):
        add(plan_ledger, "bad", lane=lane, kind=kind)


@pytest.mark.parametrize("slice_ids", ["other,unknown,plan", ", ,"])
def test_slice_refuses_bad_ids_without_finishing(plan_ledger, slice_ids):
    add(plan_ledger, "plan", lane="plan", kind="plan")
    add(plan_ledger, "other", phase="p2")
    state, rejected = update(plan_ledger, state="done", proof={"slice": slice_ids})
    assert rejected == ["finish-plan"]
    assert state["tasks"][0]["state"] == "open"
    if slice_ids == "other,unknown,plan":
        refusal = state["_meta"]["warnings"][-1]
        for task_id in ("other", "unknown", "plan"):
            assert task_id in refusal


def test_plan_cli_scaffold_reads_phase_evidence(tmp_path, monkeypatch):
    from scripts.swarm_ledger import ledger

    doc = {"overview": "Intent", "phases": [{"id": "p1", "title": "Build", "description": "Mission"}], "tasks": []}
    calls = []
    monkeypatch.setattr(ledger, "call", lambda slug: doc)
    monkeypatch.setattr(ledger, "send", lambda args, op, **fields: calls.append(fields))
    monkeypatch.setattr(ledger.ledger_workspace, "folder", lambda slug, task_id: tmp_path / task_id)
    args = ledger.build_parser().parse_args(
        [
            "--slug",
            "demo",
            "--as",
            "planner",
            "task",
            "add",
            "plan",
            "Slice build",
            "--phase",
            "p1",
            "--kind",
            "plan",
            "--lane",
            "plan",
            "--scaffold",
        ]
    )
    ledger.cmd_task(args)
    assert calls[0]["kind"] == "plan"
    assert calls[0]["phase"] == "p1"
    assert (
        (tmp_path / "plan" / "steering.md").read_text()
        == "# plan: Slice build\n\nPlan evidence\n\nProject intent\nIntent\n\nMission intent\nBuild\nMission\n"
    )
