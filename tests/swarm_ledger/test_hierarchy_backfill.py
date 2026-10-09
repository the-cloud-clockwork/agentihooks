import json

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
    assert repo.create(SLUG, legacy())
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
