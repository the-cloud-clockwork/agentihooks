import json
from pathlib import Path

from scripts.swarm_ledger import hierarchy_backfill, ledger
from scripts.swarm_ledger.repository import sqlite as store

SLUG = "legacy-hierarchy"
ARTIFACT = "http://127.0.0.1:8765/artifacts/ledger/aaaaaaaaaaaa.md"
PHASE_URL = "https://github.com/example/repo/issues/1"
TASK_URL = "https://github.com/example/repo/issues/2"


def legacy():
    return {
        "title": "Legacy",
        "overview": "Keep every task in its phase",
        "sources": [],
        "phases": [
            {"id": "p60", "title": "Build", "plan_url": PHASE_URL, "plan_ref": {"artifact": ARTIFACT}},
            {"id": "p61", "title": "Ship", "plan_url": PHASE_URL, "plan_ref": {"artifact": ARTIFACT}},
            {"id": "p62", "title": "Other"},
            {"id": "p63", "title": "More"},
        ],
        "tasks": [
            {
                "id": f"as{i}",
                "title": f"Task {i}",
                "phase": "p60",
                "plan_url": TASK_URL,
                "plan_slice": f"as{i}",
                "plan_lines": f"{i * 10}-{i * 10 + 3}",
            }
            for i in range(1, 8)
        ],
    }


def repository(tmp_path):
    repo = store.SQLiteLedgerRepository(tmp_path / store.DATABASE)
    content = legacy()
    assert repo.create(SLUG, content)
    state = repo.export_document(SLUG)
    state.update(phases=content["phases"], tasks=content["tasks"])
    repo.import_document(SLUG, state, token=repo.token(SLUG), replace=True)
    return repo


def test_dry_run_reports_all_seven_plan_mismatches_and_changes_nothing(tmp_path):
    repo = repository(tmp_path)
    before = repo.export_document(SLUG)
    token = repo.token(SLUG)
    report = hierarchy_backfill.backfill(repo, SLUG, "planner")
    assert report["applied"] is False
    assert [row["item"] for row in report["conflicts"]] == [f"tasks/as{i}" for i in range(1, 8)]
    assert all(row["kind"] == "task_plan_link" for row in report["conflicts"])
    assert report["before"] == {
        "plans": 0,
        "phases": 4,
        "slices": 0,
        "tasks": 7,
        "nodes": 11,
        "dependencies": 0,
        "phases_in_plans": 0,
        "tasks_in_phases": 7,
        "tasks_in_slices": 0,
    }
    assert report["after"] == {
        "plans": 2,
        "phases": 4,
        "slices": 7,
        "tasks": 7,
        "nodes": 20,
        "dependencies": 0,
        "phases_in_plans": 4,
        "tasks_in_phases": 7,
        "tasks_in_slices": 7,
    }
    assert repo.export_document(SLUG) == before
    assert repo.token(SLUG) == token
    assert repo.rebuild(SLUG)["drift"] == 0


def test_command_defaults_to_dry_run(tmp_path, monkeypatch, capsys):
    repo = repository(tmp_path)
    before = repo.export_document(SLUG)
    args = ledger.build_parser().parse_args(["--slug", SLUG, "--as", "planner", "hierarchy", "backfill"])
    assert args.apply is False
    monkeypatch.setattr(hierarchy_backfill, "repository", repo)
    ledger.cmd_hierarchy(args)
    assert json.loads(capsys.readouterr().out)["applied"] is False
    assert repo.export_document(SLUG) == before


def test_apply_preserves_each_phase_and_slice_range_with_zero_drift(tmp_path):
    repo = repository(tmp_path)
    before = repo.export_document(SLUG)
    token = repo.token(SLUG)
    report = hierarchy_backfill.backfill(repo, SLUG, "planner", apply=True)
    assert report["applied"] is True
    assert report["drift"]["drift"] == 0
    after = repo.export_document(SLUG)
    assert [task["phase"] for task in after["tasks"]] == ["p60"] * 7
    assert [task["plan_lines"] for task in after["tasks"]] == [f"{i * 10}-{i * 10 + 3}" for i in range(1, 8)]
    assert [task["plan_url"] for task in after["tasks"]] == [PHASE_URL] * 7
    assert after["phases"][0]["plan"] == after["phases"][1]["plan"] == "plans/plan-aaaaaaaaaaaa"
    assert after["phases"][2]["plan"] == after["phases"][3]["plan"]
    assert len(after["plans"]) == 2
    assert len(after["slices"]) == 7
    assert repo.token(SLUG) == token
    assert after["overview"] == before["overview"]
    assert after["_meta"]["rev"] == before["_meta"]["rev"] + 1
    assert repo.rebuild(SLUG)["drift"] == 0
    repeated = hierarchy_backfill.backfill(repo, SLUG, "planner", apply=True)
    assert repeated["conflicts"] == []
    assert repeated["before"] == repeated["after"]
    assert repo.export_document(SLUG) == after


def test_unphased_tasks_get_one_reported_standalone_phase(tmp_path):
    repo = repository(tmp_path)
    doc = repo.export_document(SLUG)
    doc["tasks"].extend([{"id": "loose1", "title": "Loose"}, {"id": "loose2", "title": "Loose again", "phase": ""}])
    repo.import_document(SLUG, doc, token=repo.token(SLUG), replace=True)
    dry = hierarchy_backfill.backfill(repo, SLUG, "planner")
    assignments = [row for row in dry["conflicts"] if row["kind"] == "missing_phase"]
    assert [row["item"] for row in assignments] == ["tasks/loose1", "tasks/loose2"]
    assert len({row["after"] for row in assignments}) == 1
    assert assignments[0]["after"].startswith("phases/standalone-")
    assert dry["after"]["phases"] == 5
    assert dry["after"]["tasks_in_phases"] == 9
    assert repo.export_document(SLUG) == doc
    report = hierarchy_backfill.backfill(repo, SLUG, "planner", apply=True)
    assert report["drift"]["drift"] == 0
    saved = repo.export_document(SLUG)
    assert saved["tasks"][-1]["phase"] == saved["tasks"][-2]["phase"]
    assert len(saved["plans"]) == 2


def test_recorded_ledger_copy_reports_the_known_conflicts_then_has_zero_drift(tmp_path):
    repo = repository(tmp_path)
    doc = repo.export_document(SLUG)
    content = json.loads(Path(__file__).with_name("fixtures").joinpath("hierarchy_legacy.json").read_text())
    doc.update(content)
    repo.import_document(SLUG, doc, token=repo.token(SLUG), replace=True)
    before = repo.export_document(SLUG)
    report = hierarchy_backfill.backfill(repo, SLUG, "planner")
    mismatches = {row["item"] for row in report["conflicts"] if row["kind"] == "task_plan_link"}
    assert {f"tasks/as{i}" for i in range(1, 8)} <= mismatches
    assert report["before"]["phases"] == 75
    assert report["before"]["tasks"] == 1233
    assert repo.export_document(SLUG) == before
    result = hierarchy_backfill.backfill(repo, SLUG, "planner", apply=True)
    saved = repo.export_document(SLUG)
    phases = {row["id"] for row in saved["phases"]}
    assert all(task["phase"] in phases for task in saved["tasks"])
    assert len(saved["tasks"]) == 1233
    assert len(saved["phases"]) == 76
    assert result["drift"]["drift"] == 0
    assert repo.rebuild(SLUG)["drift"] == 0
    assert hierarchy_backfill.backfill(repo, SLUG, "planner", apply=True)["conflicts"] == []
