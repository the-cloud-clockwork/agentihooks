import pytest

from scripts.swarm_ledger import ledger_core as core
from scripts.swarm_ledger import ledger_kinds, ledger_tasks, new_ledger
from tests.swarm_ledger import legacy_page  # noqa: E402
from tests.swarm_ledger.plan_slices import anchored

PLAN = "https://github.com/acme/app/issues/1"


@pytest.fixture
def plan_ledger(tmp_path, monkeypatch):
    monkeypatch.setattr(core, "LEDGER_DIR", tmp_path)
    monkeypatch.setitem(core.EXTENSION_OPS, "task_add", ledger_tasks)
    monkeypatch.setitem(core.EXTENSION_OPS, "task_update", ledger_tasks)
    content = {"title": "Plan proof", "overview": "Project intent", "sources": [], "phases": [{"title": "Build"}]}
    html, json_path = core.paths("plan-proof")
    json_path.unlink(missing_ok=True)
    html.parent.mkdir(parents=True, exist_ok=True)
    html.write_text(legacy_page.render(new_ledger.build_doc(content), "plan-proof", 8765))
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
    anchored(plan_ledger, "build")
    add(plan_ledger, "build", plan_url=PLAN, plan_slice="build")
    state, _ = update(plan_ledger, state="done")
    assert state["tasks"][0]["state"] == "open"
    state, _ = update(plan_ledger, state="done", proof={"slice": " build "})
    assert state["tasks"][0]["state"] == "done"
    assert state["tasks"][0]["proof"] == {"slice": " build "}


@pytest.mark.parametrize(("lane", "kind"), [("plan", "code"), ("eng", "plan"), ("ci", "plan")])
def test_plan_lane_accepts_only_plan_tasks(plan_ledger, lane, kind):
    with pytest.raises(
        ValueError, match="^plan tasks must use the plan lane, and the plan lane accepts only plan tasks$"
    ):
        add(plan_ledger, "bad", lane=lane, kind=kind)


@pytest.mark.parametrize("slice_ids", ["other,unknown,plan", ", ,"])
def test_slice_refuses_bad_ids_without_finishing(plan_ledger, slice_ids):
    add(plan_ledger, "plan", lane="plan", kind="plan")
    add(plan_ledger, "other", phase="p2")
    state, rejected = update(plan_ledger, state="done", proof={"slice": slice_ids})
    assert rejected == ["finish-plan"]
    assert state["tasks"][0]["state"] == "open"
    bad = "other, unknown, plan" if slice_ids == "other,unknown,plan" else "<empty>, <empty>, <empty>"
    assert state["_meta"]["warnings"][-1] == f"tasks/plan has invalid slice task ids: {bad}"


def test_plan_cli_scaffold_reads_phase_evidence(tmp_path, monkeypatch):
    from scripts.swarm_ledger import ledger

    doc = {"overview": "Intent", "phases": [{"id": "p1", "title": "Build", "description": "Mission"}], "tasks": []}
    calls = []

    def read_doc(slug):
        assert slug == "demo"
        return doc

    def work_folder(slug, task_id):
        assert (slug, task_id) == ("demo", "plan")
        return tmp_path / task_id

    monkeypatch.setattr(ledger, "call", read_doc)
    monkeypatch.setattr(ledger, "send", lambda args, op, **fields: calls.append(fields))
    monkeypatch.setattr(ledger.ledger_workspace, "folder", work_folder)
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
            "--description",
            "Slice the mission",
            "--scaffold",
        ]
    )
    ledger.cmd_task(args)
    assert calls[0]["kind"] == "plan"
    assert calls[0]["phase"] == "p1"
    assert calls[0]["workspace"] == str(tmp_path / "plan")
    assert (
        (tmp_path / "plan" / "steering.md").read_text()
        == "# plan: Slice build\n\nSlice the mission\n\nPlan evidence\n\nProject intent\nIntent\n\nMission intent\nBuild\nMission\n"
    )


def test_task_command_lane_defaults_and_choices():
    from scripts.swarm_ledger import ledger

    parser = ledger.build_parser()
    base = ["--slug", "demo", "--as", "planner", "task", "add", "build", "Build"]
    assert parser.parse_args(base).lane == "eng"
    for lane in ("eng", "ci", "plan"):
        assert parser.parse_args([*base, "--lane", lane]).lane == lane
    with pytest.raises(SystemExit):
        parser.parse_args([*base, "--lane", "unknown"])


def test_a_plan_accepts_multiple_phase_tasks_and_open_updates(plan_ledger):
    add(plan_ledger, "plan", lane="plan", kind="plan")
    anchored(plan_ledger, "first", "second")
    add(plan_ledger, "first", plan_url=PLAN, plan_slice="first")
    add(plan_ledger, "second", lane="ci", kind="ci", plan_url=PLAN, plan_slice="second")
    state, rejected = update(plan_ledger, state="claimed")
    assert rejected == []
    assert state["tasks"][0]["state"] == "claimed"
    state, rejected = update(plan_ledger, state="done", proof={"slice": "first, second"})
    assert rejected == []
    assert state["tasks"][0]["state"] == "done"


def test_a_slice_without_its_anchor_or_range_is_refused_by_name(plan_ledger):
    anchored(plan_ledger, "first")
    state, rejected = add(plan_ledger, "lost", plan_slice="lost")
    assert rejected == ["add-lost"]
    assert state["_meta"]["warnings"] == ["slice anchor lost is missing or repeated in its phase"]
    assert [t["id"] for t in state["tasks"]] == []
    add(plan_ledger, "plan", lane="plan", kind="plan")
    add(plan_ledger, "first", plan_url=PLAN, plan_slice="first")
    add(plan_ledger, "second", plan_url=PLAN, by="swarm")
    add(plan_ledger, "third", plan_url=PLAN, by="swarm")
    state, rejected = update(plan_ledger, state="done", proof={"slice": "first, second, third"})
    assert rejected == ["finish-plan"]
    assert state["tasks"][0]["state"] == "open"
    assert state["_meta"]["warnings"] == ["tasks/plan slice tasks lack valid plan ranges or anchors: second, third"]


def test_ordinary_tasks_can_still_finish(plan_ledger):
    add(plan_ledger, "plan")
    state, rejected = update(plan_ledger, state="done")
    assert rejected == []
    assert state["tasks"][0]["state"] == "done"


@pytest.mark.parametrize("destination", ["research", "code"])
def test_plan_kind_change_updates_lane_and_workspace(plan_ledger, tmp_path, destination):
    from scripts.swarm_ledger import ledger_workspace

    workspace = tmp_path / "work"
    workspace.mkdir()
    add(plan_ledger, "plan", kind="plan", lane="plan", workspace=str(workspace))
    before = core.sync(plan_ledger)[0]
    task = before["tasks"][0]
    (workspace / "steering.md").write_text(ledger_workspace.steering(task, before), encoding="utf-8")
    (workspace / "progress.md").write_text("Progress preserved\n", encoding="utf-8")
    (workspace / "proof.md").write_text("Proof preserved\n", encoding="utf-8")
    assert "Plan evidence" in (workspace / "steering.md").read_text()

    state, rejected = update(plan_ledger, kind=destination)
    assert rejected == []
    task = state["tasks"][0]
    assert task["kind"] == destination
    assert task["lane"] == "eng"
    assert (workspace / "steering.md").read_text() == "# plan: Build a feature\n"
    assert (workspace / "progress.md").read_text() == "Progress preserved\n"
    assert (workspace / "proof.md").read_text() == "Proof preserved\n"
    assert core.sync(plan_ledger)[0]["tasks"][0] == task


@pytest.mark.parametrize(
    ("task", "fields", "expected"),
    [
        ({"kind": "plan", "lane": "plan"}, {"kind": "research"}, {"kind": "research", "lane": "eng"}),
        ({"kind": "plan", "lane": "plan"}, {"kind": "code", "lane": "ci"}, {"kind": "code", "lane": "ci"}),
        ({"kind": "plan", "lane": "plan"}, {"kind": "plan"}, {"kind": "plan"}),
        ({"kind": "plan", "lane": "plan"}, {"title": "Revised"}, {"title": "Revised"}),
        ({"kind": "code", "lane": "ci"}, {"kind": "research"}, {"kind": "research"}),
    ],
)
def test_kind_update_preserves_explicit_and_unrelated_fields(task, fields, expected):
    original = dict(fields)
    assert ledger_tasks._update_fields(task, fields) == expected
    assert fields == original


def test_plan_kind_change_without_workspace_and_same_kind(plan_ledger):
    add(plan_ledger, "plan", kind="plan", lane="plan")
    state, rejected = update(plan_ledger, kind="plan")
    assert rejected == []
    assert state["tasks"][0]["lane"] == "plan"
    state, rejected = core.sync(
        plan_ledger,
        ops=[
            {
                "op": "task_update",
                "id": "convert",
                "by": "planner",
                "item": "tasks/plan",
                "fields": {"kind": "research"},
            }
        ],
    )
    assert rejected == []
    assert state["tasks"][0]["kind"] == "research"
    assert state["tasks"][0]["lane"] == "eng"


def test_same_plan_kind_keeps_workspace_notes(plan_ledger, tmp_path):
    workspace = tmp_path / "work"
    workspace.mkdir()
    add(plan_ledger, "plan", kind="plan", lane="plan", workspace=str(workspace))
    (workspace / "steering.md").write_text("Agent notes\n", encoding="utf-8")
    state, rejected = update(plan_ledger, kind="plan")
    assert rejected == []
    assert state["tasks"][0]["kind"] == "plan"
    assert (workspace / "steering.md").read_text() == "Agent notes\n"


def test_workspace_rewrite_uses_utf8_in_an_ascii_locale(tmp_path):
    import os
    import subprocess
    import sys

    code = (
        "from scripts.swarm_ledger import ledger_workspace; "
        f'ledger_workspace.rewrite({{"id": "plan", "title": "\\u00f1", "workspace": {str(tmp_path)!r}}})'
    )
    subprocess.run(
        [sys.executable, "-c", code],
        env={**os.environ, "LC_ALL": "C", "PYTHONUTF8": "0", "PYTHONCOERCECLOCALE": "0"},
        check=True,
        capture_output=True,
    )
    assert (tmp_path / "steering.md").read_bytes() == "# plan: ñ\n".encode("utf-8")


def test_workspace_rewrite_keeps_unicode_with_an_ascii_default(tmp_path, monkeypatch):
    from pathlib import Path

    from scripts.swarm_ledger import ledger_workspace

    write_text = Path.write_text

    def ascii_default(path, text, encoding=None):
        return write_text(path, text, encoding=encoding or "ascii")

    monkeypatch.setattr(Path, "write_text", ascii_default)
    ledger_workspace.rewrite({"id": "plan", "title": "ñ", "workspace": str(tmp_path)})
    assert (tmp_path / "steering.md").read_bytes() == "# plan: ñ\n".encode("utf-8")
