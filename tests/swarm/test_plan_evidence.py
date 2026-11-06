from scripts.swarm_ledger import ledger_workspace


def test_planner_steering_carries_transitive_evidence(tmp_path, monkeypatch):
    task = {"id": "plan-current", "title": "Slice current", "kind": "plan", "phase": "p3"}
    doc = {
        "overview": "Project intent",
        "phases": [
            {
                "id": "p1",
                "title": "First phase",
                "description": "First mission",
                "comments": [{"text": f"comment {i}"} for i in range(7)],
            },
            {"id": "p2", "title": "Second phase", "depends_on": ["p1"]},
            {
                "id": "p3",
                "title": "Current phase",
                "description": "Current mission",
                "depends_on": ["p2"],
                "review": {"state": "sent_back", "note": "Split the large task"},
            },
        ],
        "tasks": [
            {
                "id": "first",
                "phase": "p1",
                "title": "First build",
                "state": "done",
                "pr_url": "https://example.com/first",
                "proof": {"output": "First verified" + "x" * 2000},
            },
            {
                "id": "second",
                "phase": "p2",
                "title": "Second build",
                "state": "done",
                "proof": {"output": "Second verified"},
            },
            {"id": "unrelated", "phase": "p4", "title": "Unrelated build"},
        ],
    }
    monkeypatch.setattr(ledger_workspace, "folder", lambda slug, task_id: tmp_path / task_id)
    folder = ledger_workspace.scaffold("demo", task, doc)
    text = (folder / "steering.md").read_text()
    assert (folder / "progress.md").read_text() == ""
    assert (folder / "proof.md").read_text() == ""
    for expected in (
        "Project intent",
        "Current phase",
        "Current mission",
        "First phase",
        "First build",
        "done",
        "https://example.com/first",
        "First verified",
        "Second build",
        "Second verified",
        "Split the large task",
        "comment 2",
        "comment 6",
    ):
        assert expected in text
    assert "Unrelated build" not in text
    assert "comment 1" not in text
    monkeypatch.setenv("AGENTIHOOKS_PLAN_EVIDENCE_CHARS", "100")
    capped = ledger_workspace.steering(task, doc)
    assert "cut" in capped.lower()
    assert len(capped) < len(text)
    (folder / "progress.md").write_text("Existing progress")
    doc["phases"][2]["review"]["note"] = "Revised slice requested"
    ledger_workspace.scaffold("demo", task, doc)
    assert "Revised slice requested" in (folder / "steering.md").read_text()
    assert (folder / "progress.md").read_text() == "Existing progress"


def test_planner_evidence_default_cap_and_dependency_comments(monkeypatch):
    monkeypatch.delenv("AGENTIHOOKS_PLAN_EVIDENCE_CHARS", raising=False)
    task = {"id": "plan", "kind": "plan", "phase": "build"}
    doc = {
        "overview": "Intent",
        "phases": [
            {"id": "build", "title": "Build", "depends_on": ["base", "base"]},
            {
                "id": "base",
                "title": "Base",
                "depends_on": ["build"],
                "comments": [{"text": "Kept"}, {"text": "Deleted", "deleted": True}],
            },
        ],
        "tasks": [
            {
                "phase": "base",
                "title": "Proof",
                "state": "done",
                "pr_url": "https://example.com/pr",
                "proof": {"output": "x" * 7000},
            }
        ],
    }
    text = ledger_workspace.steering(task, doc)
    assert "Cut Dependency tasks for Base: 1098 characters omitted" in text
    assert text.count("Dependency tasks for Base\n") == 1
    assert "Kept" in text and "Deleted" not in text
    assert "Review note" not in text
    assert "x" * 6000 not in text


def test_plan_evidence_text_includes_each_dependency_and_review():
    task = {"id": "plan", "title": "Slice", "kind": "plan", "phase": "current"}
    doc = {
        "overview": "Intent",
        "phases": [
            {
                "id": "current",
                "title": "Current",
                "description": "Mission",
                "depends_on": ["a", "a", "b"],
                "review": {"state": "sent_back", "note": "Revise"},
            },
            {"id": "a", "title": "Alpha", "depends_on": ["current"], "comments": [{"text": "One"}, {"text": "Two"}]},
            {"id": "b", "title": "Beta"},
        ],
        "tasks": [
            {"phase": "a", "title": "Diseño", "state": "done"},
            {
                "phase": "b",
                "title": "Build",
                "state": "pr",
                "pr_url": "https://example.com/pr",
                "proof": {"output": "Passed"},
            },
        ],
    }
    assert ledger_workspace.steering(task, doc) == (
        "# plan: Slice\n\nPlan evidence\n\nProject intent\nIntent\n\nMission intent\nCurrent\nMission\n\n"
        'Dependency tasks for Alpha\n[{"title": "Diseño", "state": "done", "pr_url": "", "proof": ""}]\n\n'
        "Last five comments for Alpha\nOne\nTwo\n\n"
        'Dependency tasks for Beta\n[{"title": "Build", "state": "pr", "pr_url": "https://example.com/pr", "proof": {"output": "Passed"}}]\n\n'
        "Last five comments for Beta\n\n\nReview note\nRevise\n"
    )


def test_plan_evidence_without_dependencies_or_review_note():
    task = {"id": "plan", "title": "Slice", "kind": "plan", "phase": "current"}
    doc = {
        "overview": "Intent",
        "phases": [{"id": "current", "title": "Current", "review": {"state": "sent_back"}}],
        "tasks": [],
    }
    assert (
        ledger_workspace.steering(task, doc)
        == "# plan: Slice\n\nPlan evidence\n\nProject intent\nIntent\n\nMission intent\nCurrent\n\n\nReview note\n\n"
    )


def test_evidence_at_exact_limit_is_complete(monkeypatch):
    monkeypatch.setenv("AGENTIHOOKS_PLAN_EVIDENCE_CHARS", "6")
    task = {"id": "plan", "title": "Slice", "kind": "plan", "phase": "current"}
    doc = {"overview": "Intent", "phases": [{"id": "current", "title": "Now", "description": "Go"}], "tasks": []}
    assert (
        ledger_workspace.steering(task, doc)
        == "# plan: Slice\n\nPlan evidence\n\nProject intent\nIntent\n\nMission intent\nNow\nGo\n"
    )


def test_planner_steering_uses_utf8_in_an_ascii_locale(tmp_path, monkeypatch):
    import os
    import subprocess
    import sys

    monkeypatch.setattr(ledger_workspace, "folder", lambda slug, task: tmp_path)
    ledger_workspace.scaffold(
        "demo",
        {"id": "plan", "kind": "plan", "phase": "p"},
        {"overview": "ñ", "phases": [{"id": "p", "title": "Build"}], "tasks": []},
    )
    code = (
        "from pathlib import Path; from scripts.swarm_ledger import ledger_workspace; "
        f"ledger_workspace.folder = lambda slug, task: Path({str(tmp_path)!r}); "
        'ledger_workspace.scaffold("demo", {"id": "plan", "kind": "plan", "phase": "p"}, '
        '{"overview": "\\u00f1", "phases": [{"id": "p", "title": "Build"}], "tasks": []})'
    )
    subprocess.run(
        [sys.executable, "-c", code],
        env={**os.environ, "LC_ALL": "C", "PYTHONUTF8": "0", "PYTHONCOERCECLOCALE": "0"},
        check=True,
        capture_output=True,
    )
    assert b"\xc3\xb1" in (tmp_path / "steering.md").read_bytes()


PLAN = (
    "# Plan\n\n## Ingest\n\n<!-- slice: one -->\n### one\n- First slice\n\n"
    "<!-- slice: two -->\n### two\n- Second slice\n\n## Later\n- Not this phase\n"
)
PLAN_URL = "https://ledger.example/artifacts/demo/plan-file"


def _ingested(lines: str = "3-12") -> tuple[dict, dict]:
    task = {"id": "plan", "title": "Slice", "kind": "plan", "phase": "ingest"}
    doc = {
        "overview": "Intent",
        "phases": [{"id": "ingest", "title": "Ingest", "plan_ref": {"artifact": PLAN_URL, "lines": lines}}],
        "tasks": [],
        "artifacts": [{"plan": True, "file": {"id": "plan-file"}}],
    }
    return task, doc


def test_planner_steering_carries_the_phase_plan_chunk_and_slice_names(monkeypatch):
    from scripts.swarm_ledger import ledger_artifacts

    monkeypatch.delenv("AGENTIHOOKS_PLAN_EVIDENCE_CHARS", raising=False)
    monkeypatch.setattr(ledger_artifacts, "read", lambda slug, file_id: PLAN.encode())
    task, doc = _ingested()
    text = ledger_workspace.steering(task, doc)
    assert (
        "Phase plan\nRead the whole phase plan with agentihooks plan read --phase ingest.\n"
        "## Ingest\n\n<!-- slice: one -->\n### one\n- First slice\n\n<!-- slice: two -->\n### two\n- Second slice\n"
    ) in text
    assert (
        "Slice names\nThe phase plan is already published. Add one task per slice with task add --plan-slice "
        "<name> and do not publish it again: one, two"
    ) in text
    assert "Not this phase" not in text


def test_phase_plan_chunk_is_bounded(monkeypatch):
    from scripts.swarm_ledger import ledger_artifacts

    monkeypatch.setenv("AGENTIHOOKS_PLAN_EVIDENCE_CHARS", "80")
    monkeypatch.setattr(ledger_artifacts, "read", lambda slug, file_id: PLAN.encode())
    task, doc = _ingested()
    text = ledger_workspace.steering(task, doc)
    assert "[Cut Phase plan:" in text
    assert "[Cut Slice names:" in text
    assert "- Second slice" not in text


def test_phase_plan_without_slice_anchors_says_none(monkeypatch):
    from scripts.swarm_ledger import ledger_artifacts

    monkeypatch.setattr(ledger_artifacts, "read", lambda slug, file_id: PLAN.encode())
    task, doc = _ingested("13-14")
    text = ledger_workspace.steering(task, doc)
    assert "## Later\n- Not this phase\n" in text
    assert (
        "Slice names\nNone: the published phase plan holds no slice anchors, so write the slice as a plan with "
        "anchors and publish it before adding tasks.\n"
    ) in text


def test_unreadable_phase_plan_names_the_reason(monkeypatch):
    task, doc = _ingested()
    doc["artifacts"] = []
    text = ledger_workspace.steering(task, doc)
    assert (
        "Phase plan\nRead the whole phase plan with agentihooks plan read --phase ingest.\n"
        "It cannot be read here: plan artifact is missing or is not marked as a plan\n"
    ) in text
