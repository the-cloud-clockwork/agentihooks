import json
from collections import Counter
from pathlib import Path

import pytest

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
    issues = "https://github.com/the-cloud-clockwork/agentihooks/issues"
    assert [
        (row["item"], row["before"], row["after"])
        for row in report["conflicts"]
        if row["item"] in {f"tasks/as{i}" for i in range(1, 8)}
    ] == [(f"tasks/as{i}", f"{issues}/1866", f"{issues}/1877") for i in range(1, 8)]
    assert Counter(row["kind"] for row in report["conflicts"]) == {"missing_phase": 41, "task_plan_link": 14}
    assert report["refused"] == ""
    assert report["before"] == {
        "plans": 0,
        "phases": 75,
        "slices": 0,
        "tasks": 1233,
        "nodes": 1308,
        "dependencies": 1195,
        "phases_in_plans": 0,
        "tasks_in_phases": 1192,
        "tasks_in_slices": 0,
    }
    assert report["after"] == {
        "plans": 19,
        "phases": 76,
        "slices": 251,
        "tasks": 1233,
        "nodes": 1579,
        "dependencies": 1195,
        "phases_in_plans": 76,
        "tasks_in_phases": 1233,
        "tasks_in_slices": 262,
    }
    assert repo.export_document(SLUG) == before
    result = hierarchy_backfill.backfill(repo, SLUG, "planner", apply=True)
    saved = repo.export_document(SLUG)
    phases = {row["id"] for row in saved["phases"]}
    assert all(isinstance(task["phase"], str) and task["phase"] in phases for task in saved["tasks"])
    assert all(row["plan"].startswith("plans/") for row in saved["phases"])
    assert len(saved["tasks"]) == 1233
    assert len(saved["phases"]) == 76
    assert result["drift"]["drift"] == 0
    assert repo.rebuild(SLUG)["drift"] == 0
    assert hierarchy_backfill.backfill(repo, SLUG, "planner", apply=True)["conflicts"] == []


def test_dry_run_on_an_old_database_does_not_create_hierarchy_tables(tmp_path):
    repo = repository(tmp_path)
    before = repo.export_document(SLUG)
    with repo.connect() as connection, connection:
        connection.execute("DROP TABLE work_dependencies")
        connection.execute("DROP TABLE work_nodes")
    reopened = store.SQLiteLedgerRepository(repo.path)
    report = hierarchy_backfill.backfill(reopened, SLUG, "planner")
    assert report["applied"] is False
    assert report["drift"]["drift"] == 11
    assert len(report["drift"]["missing_nodes"]) == 11
    with store.read_only(repo.path.parent) as connection:
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "work_nodes" not in tables
    assert "work_dependencies" not in tables
    assert store.read_document(repo.path.parent, SLUG) == before
    applied = hierarchy_backfill.backfill(reopened, SLUG, "planner", apply=True)
    assert applied["drift"]["drift"] == 0


def test_failed_apply_rolls_back_document_and_hierarchy_together(tmp_path, monkeypatch):
    repo = repository(tmp_path)
    before = repo.export_document(SLUG)
    original = repo._write

    def fail(*args, **kwargs):
        original(*args, **kwargs)
        raise RuntimeError("write failed")

    monkeypatch.setattr(repo, "_write", fail)
    with pytest.raises(RuntimeError, match="^write failed$"):
        hierarchy_backfill.backfill(repo, SLUG, "planner", apply=True)
    reopened = store.SQLiteLedgerRepository(repo.path)
    assert reopened.export_document(SLUG) == before
    assert reopened.rebuild(SLUG)["drift"] == 0


def test_existing_explicit_plan_parents_are_preserved_without_legacy_links(tmp_path):
    repo = repository(tmp_path)
    doc = repo.export_document(SLUG)
    doc["plans"] = [{"id": "a", "title": "Existing A"}, {"id": "b", "title": "Existing B"}]
    doc["phases"] = [
        {"id": "p1", "title": "Existing first", "plan": "plans/a"},
        {"id": "p2", "title": "Existing second", "plan": "plans/b"},
    ]
    doc["tasks"] = []
    repo.import_document(SLUG, doc, token=repo.token(SLUG), replace=True)
    report = hierarchy_backfill.backfill(repo, SLUG, "planner", apply=True)
    assert report["conflicts"] == []
    assert repo.export_document(SLUG)["phases"] == doc["phases"]
    assert repo.export_document(SLUG)["plans"] == doc["plans"]


STANDALONE = "standalone-4028096bb707"
KEPT = "http://h/a/kept.md"
NEW = "http://h/b/dddddddddddd.md"
TAKEN = "http://other/x/cccccccccccc.md"
SAME_STEM = "http://other/y/cccccccccccc.md"
U1, U2, U3, MOVED, BAD, TWIN_URL = (f"https://github.com/o/r/issues/{n}" for n in (1, 2, 3, 4, 9, 7))
TWIN = "http://h/t/twin.md"


def branches():
    return {
        "plans": [
            {"id": "kept", "title": "Kept", "artifact": KEPT, "url": ""},
            {"id": "plan-cccccccccccc", "title": "Taken", "artifact": TAKEN, "url": ""},
            {"id": "kept2", "title": "Kept two", "artifact": "", "url": U2},
            {"id": "kept3", "title": "Kept three", "artifact": "", "url": U3},
            {"id": "twin-a", "title": "Twin A", "artifact": TWIN, "url": TWIN_URL},
            {"id": "twin-b", "title": "Twin B", "artifact": TWIN, "url": TWIN_URL},
        ],
        "slices": [
            {"id": "plan-dddddddddddd.s1.p5.t8", "phase": "phases/p5", "anchor": "s1", "lines": "1-2"},
            {"id": "custom", "phase": "phases/p1", "anchor": "s9", "lines": "7-8"},
            {"id": "custom2", "phase": "phases/p1", "anchor": "s8"},
            {"id": "kept.s7", "phase": "phases/p1", "anchor": "s7"},
        ],
        "phases": [
            {"id": "p2", "title": "Url moves", "plan": "plans/kept", "plan_url": MOVED},
            {
                "id": "p1",
                "title": "Artifact kept",
                "plan": "plans/kept",
                "plan_ref": {"artifact": KEPT},
                "plan_url": U1,
            },
            {"id": "p3", "title": "Url kept", "plan": "plans/kept2", "plan_url": U2},
            {"id": "p4", "title": "New artifact", "plan_ref": {"artifact": NEW}},
            {"id": "p5", "title": "Same artifact", "plan_ref": {"artifact": NEW}, "plan_url": U1},
            {"id": "p6", "title": "Stem taken", "plan_ref": {"artifact": SAME_STEM}},
            {"id": "p7", "title": "Unlinked"},
            {"id": "p8", "title": "Unlinked again"},
            {"id": "p9", "title": "Url found", "plan_url": U2},
            {"id": "p10", "title": "Url plan", "plan": "plans/kept3"},
            {"id": "p11", "title": "Artifact plan", "plan": "plans/kept"},
            {
                "id": "p12",
                "title": "Twin artifact",
                "plan": "plans/twin-b",
                "plan_ref": {"artifact": TWIN},
                "plan_url": TWIN,
            },
            {"id": "p13", "title": "Twin url", "plan": "plans/twin-b", "plan_url": TWIN_URL},
        ],
        "tasks": [
            {"id": "t1", "title": "", "phase": "p1", "plan_url": KEPT, "plan_slice": "s1", "plan_lines": "1-2"},
            {"id": "t2", "title": "", "phase": "p1", "plan_url": BAD, "plan_slice": "s1", "plan_lines": "1-2"},
            {"id": "t3", "title": "", "phase": "p1", "plan_slice": "s1", "plan_lines": "5-6"},
            {"id": "t4", "title": "", "phase": "p1", "slice": "slices/kept.s1"},
            {"id": "t5", "title": "", "phase": "p1", "slice": "slices/ghost"},
            {"id": "t6", "title": "", "phase": "p1", "plan_slice": "s2", "slice": "slices/old"},
            {"id": "t7", "title": "", "phase": "p4", "plan_slice": "s1", "plan_lines": "1-2"},
            {"id": "t8", "title": "", "phase": "p5", "plan_slice": "s1", "plan_lines": "1-2"},
            {"id": "t9", "title": "", "phase": "p5", "plan_slice": "s1", "plan_lines": "3-4"},
            {"id": "t10", "title": "", "phase": "p7", "plan_url": BAD},
            {"id": "t11", "title": "", "phase": "p10", "plan_url": BAD},
            {"id": "t12", "title": "", "phase": "p11", "plan_url": BAD},
            {"id": "t13", "title": "", "phase": "ghost"},
            {"id": "t14", "title": ""},
            {"id": "t15", "title": "", "phase": ""},
            {"id": "t16", "title": "", "phase": "p4", "slice": "slices/kept.s1"},
            {
                "id": "t17",
                "title": "",
                "phase": "p1",
                "plan_slice": "s9",
                "plan_lines": "7-8",
                "slice": "slices/custom",
            },
            {
                "id": "t18",
                "title": "",
                "phase": "p1",
                "plan_slice": "s9",
                "plan_lines": "9-9",
                "slice": "slices/custom",
            },
            {"id": "t19", "title": "", "phase": "p1", "plan_slice": "s8", "slice": "slices/custom2"},
            {"id": "t20", "title": "", "phase": "p1", "plan_slice": "s7"},
        ],
    }


def test_preview_places_every_legacy_shape_and_reports_each_conflict_in_order():
    doc = branches()
    after, report = hierarchy_backfill.preview(doc, SLUG)
    assert doc == branches()
    assert after["plans"] == [
        *branches()["plans"],
        {"id": "plan-dddddddddddd", "title": "New artifact", "artifact": NEW, "url": ""},
        {"id": "plan-cccccccccccc-3e8a5e5522a1", "title": "Stem taken", "artifact": SAME_STEM, "url": ""},
        {"id": "plan-9c18a4c47d37", "title": "Url moves", "artifact": "", "url": MOVED},
        {"id": STANDALONE, "title": "Standalone", "artifact": "", "url": ""},
    ]
    assert after["slices"] == [
        *branches()["slices"],
        {"id": "kept.s1", "phase": "phases/p1", "anchor": "s1", "lines": "1-2"},
        {"id": "kept.s1.p1.t3", "phase": "phases/p1", "anchor": "s1", "lines": "5-6"},
        {"id": "kept.s2", "phase": "phases/p1", "anchor": "s2", "lines": ""},
        {"id": "plan-dddddddddddd.s1", "phase": "phases/p4", "anchor": "s1", "lines": "1-2"},
        {"id": "plan-dddddddddddd.s1.p5.t9", "phase": "phases/p5", "anchor": "s1", "lines": "3-4"},
        {"id": "kept.s9", "phase": "phases/p1", "anchor": "s9", "lines": "9-9"},
    ]
    assert [(row["id"], row["plan"], row.get("plan_url")) for row in after["phases"]] == [
        ("p2", "plans/plan-9c18a4c47d37", MOVED),
        ("p1", "plans/kept", KEPT),
        ("p3", "plans/kept2", U2),
        ("p4", "plans/plan-dddddddddddd", None),
        ("p5", "plans/plan-dddddddddddd", NEW),
        ("p6", "plans/plan-cccccccccccc-3e8a5e5522a1", None),
        ("p7", f"plans/{STANDALONE}", None),
        ("p8", f"plans/{STANDALONE}", None),
        ("p9", "plans/kept2", U2),
        ("p10", "plans/kept3", None),
        ("p11", "plans/kept", None),
        ("p12", "plans/twin-b", TWIN),
        ("p13", "plans/twin-b", TWIN_URL),
        (STANDALONE, f"plans/{STANDALONE}", None),
    ]
    assert after["phases"][-1] == {
        "id": STANDALONE,
        "title": "Standalone",
        "done": False,
        "plan": f"plans/{STANDALONE}",
    }
    assert [(row["id"], row["phase"], row.get("slice"), row.get("plan_url")) for row in after["tasks"]] == [
        ("t1", "p1", "slices/kept.s1", KEPT),
        ("t2", "p1", "slices/kept.s1", KEPT),
        ("t3", "p1", "slices/kept.s1.p1.t3", None),
        ("t4", "p1", "slices/kept.s1", None),
        ("t5", "p1", None, None),
        ("t6", "p1", "slices/kept.s2", None),
        ("t7", "p4", "slices/plan-dddddddddddd.s1", None),
        ("t8", "p5", "slices/plan-dddddddddddd.s1.p5.t8", None),
        ("t9", "p5", "slices/plan-dddddddddddd.s1.p5.t9", None),
        ("t10", "p7", None, ""),
        ("t11", "p10", None, U3),
        ("t12", "p11", None, KEPT),
        ("t13", "ghost", None, None),
        ("t14", STANDALONE, None, None),
        ("t15", STANDALONE, None, None),
        ("t16", "p4", None, None),
        ("t17", "p1", "slices/custom", None),
        ("t18", "p1", "slices/kept.s9", None),
        ("t19", "p1", "slices/custom2", None),
        ("t20", "p1", "slices/kept.s7", None),
    ]
    assert [(row["kind"], row["item"], row["before"], row["after"]) for row in report["conflicts"]] == [
        ("missing_phase", "tasks/t14", None, f"phases/{STANDALONE}"),
        ("missing_phase", "tasks/t15", "", f"phases/{STANDALONE}"),
        ("phase_plan_link", "phases/p1", U1, KEPT),
        ("phase_plan_link", "phases/p5", U1, NEW),
        ("plan_collision", "phases/p6", "plans/plan-cccccccccccc", "plans/plan-cccccccccccc-3e8a5e5522a1"),
        ("phase_plan", "phases/p2", "plans/kept", "plans/plan-9c18a4c47d37"),
        ("task_plan_link", "tasks/t2", BAD, KEPT),
        ("slice_collision", "tasks/t3", "slices/kept.s1", "phases/p1"),
        ("task_slice", "tasks/t5", "slices/ghost", ""),
        ("task_slice", "tasks/t6", "slices/old", "slices/kept.s2"),
        ("slice_collision", "tasks/t8", "slices/plan-dddddddddddd.s1", "phases/p5"),
        ("slice_collision", "tasks/t9", "slices/plan-dddddddddddd.s1", "phases/p5"),
        ("task_plan_link", "tasks/t10", BAD, ""),
        ("task_plan_link", "tasks/t11", BAD, U3),
        ("task_plan_link", "tasks/t12", BAD, KEPT),
        ("missing_phase", "tasks/t13", "ghost", None),
        ("task_slice", "tasks/t16", "slices/kept.s1", ""),
        ("task_slice", "tasks/t18", "slices/custom", "slices/kept.s9"),
    ]
    assert report["applied"] is False
    assert report["refused"] == "tasks/t13 names phases/ghost, which does not exist"
    assert report["before"] == {
        "plans": 6,
        "phases": 13,
        "slices": 4,
        "tasks": 20,
        "nodes": 43,
        "dependencies": 0,
        "phases_in_plans": 7,
        "tasks_in_phases": 17,
        "tasks_in_slices": 7,
    }
    assert report["after"] == {
        "plans": 10,
        "phases": 14,
        "slices": 10,
        "tasks": 20,
        "nodes": 54,
        "dependencies": 0,
        "phases_in_plans": 14,
        "tasks_in_phases": 19,
        "tasks_in_slices": 12,
    }


def test_preview_fills_collections_an_old_document_never_stored():
    after, report = hierarchy_backfill.preview({"tasks": [{"id": "t1", "title": ""}]}, SLUG)
    assert after == {
        "tasks": [{"id": "t1", "title": "", "phase": STANDALONE}],
        "plans": [{"id": STANDALONE, "title": "Standalone", "artifact": "", "url": ""}],
        "slices": [],
        "phases": [{"id": STANDALONE, "title": "Standalone", "done": False, "plan": f"plans/{STANDALONE}"}],
    }
    assert report["before"] == {
        "plans": 0,
        "phases": 0,
        "slices": 0,
        "tasks": 1,
        "nodes": 1,
        "dependencies": 0,
        "phases_in_plans": 0,
        "tasks_in_phases": 0,
        "tasks_in_slices": 0,
    }
    after, report = hierarchy_backfill.preview({"phases": [{"id": "p1", "title": "One"}]}, SLUG)
    assert after == {
        "phases": [{"id": "p1", "title": "One", "plan": f"plans/{STANDALONE}"}],
        "plans": [{"id": STANDALONE, "title": "Standalone", "artifact": "", "url": ""}],
        "slices": [],
    }
    assert report == {
        "applied": False,
        "before": {
            "plans": 0,
            "phases": 1,
            "slices": 0,
            "tasks": 0,
            "nodes": 1,
            "dependencies": 0,
            "phases_in_plans": 0,
            "tasks_in_phases": 0,
            "tasks_in_slices": 0,
        },
        "after": {
            "plans": 1,
            "phases": 1,
            "slices": 0,
            "tasks": 0,
            "nodes": 2,
            "dependencies": 0,
            "phases_in_plans": 1,
            "tasks_in_phases": 0,
            "tasks_in_slices": 0,
        },
        "conflicts": [],
        "refused": "",
    }


def test_refusal_names_every_task_whose_phase_does_not_exist():
    doc = {
        "phases": [{"id": "p1", "title": "One"}],
        "tasks": [{"id": "a", "title": "", "phase": "gone"}, {"id": "b", "title": "", "phase": "lost"}],
    }
    _, report = hierarchy_backfill.preview(doc, SLUG)
    assert report["refused"] == (
        "tasks/a names phases/gone, which does not exist; tasks/b names phases/lost, which does not exist"
    )


def dependent(tmp_path):
    repo = store.SQLiteLedgerRepository(tmp_path / store.DATABASE)
    assert repo.create(
        SLUG, {"title": "L", "overview": "o", "sources": [], "phases": [{"id": "p1", "title": "One"}], "tasks": []}
    )
    doc = repo.export_document(SLUG)
    doc["tasks"] = [
        {"id": "t1", "title": "", "phase": "ghost"},
        {"id": "t2", "title": "", "phase": "p1", "depends_on": ["t1"]},
    ]
    repo.import_document(SLUG, doc, token=repo.token(SLUG), replace=True)
    return repo


def test_dry_run_compares_the_stored_rows_and_apply_refuses_a_task_without_its_phase(tmp_path):
    repo = dependent(tmp_path)
    before = repo.export_document(SLUG)
    token = repo.token(SLUG)
    dry = hierarchy_backfill.backfill(repo, SLUG, "planner")
    assert dry == {
        "applied": False,
        "before": {
            "plans": 0,
            "phases": 1,
            "slices": 0,
            "tasks": 2,
            "nodes": 3,
            "dependencies": 1,
            "phases_in_plans": 0,
            "tasks_in_phases": 1,
            "tasks_in_slices": 0,
        },
        "after": {
            "plans": 1,
            "phases": 1,
            "slices": 0,
            "tasks": 2,
            "nodes": 4,
            "dependencies": 1,
            "phases_in_plans": 1,
            "tasks_in_phases": 1,
            "tasks_in_slices": 0,
        },
        "conflicts": [{"kind": "missing_phase", "item": "tasks/t1", "before": "ghost", "after": None}],
        "refused": "tasks/t1 names phases/ghost, which does not exist",
        "drift": {
            "missing_nodes": [],
            "extra_nodes": [],
            "changed_nodes": [],
            "missing_dependencies": [],
            "extra_dependencies": [],
            "drift": 0,
        },
    }
    del dry["drift"]
    with pytest.raises(ValueError) as refused:
        hierarchy_backfill.backfill(repo, SLUG, "planner", apply=True)
    assert str(refused.value) == json.dumps(dry, sort_keys=True)
    assert repo.export_document(SLUG) == before
    assert repo.token(SLUG) == token


def test_dry_run_reports_and_apply_refuses_a_moved_phase_that_keeps_another_plans_slices(tmp_path):
    repo = repository(tmp_path)
    doc = repo.export_document(SLUG)
    doc["plans"] = [{"id": "kept", "title": "Kept"}]
    doc["phases"] = [{"id": "p2", "title": "Moves", "plan": "plans/kept", "plan_url": MOVED}]
    doc["slices"] = [{"id": "kept.x", "phase": "phases/p2", "anchor": "x"}]
    doc["tasks"] = []
    repo.import_document(SLUG, doc, token=repo.token(SLUG), replace=True)
    before = repo.export_document(SLUG)
    dry = hierarchy_backfill.backfill(repo, SLUG, "planner")
    assert dry["refused"] == "phase p2 holds slices of another plan: kept.x"
    assert dry["conflicts"] == [
        {"kind": "phase_plan", "item": "phases/p2", "before": "plans/kept", "after": "plans/plan-9c18a4c47d37"}
    ]
    del dry["drift"]
    with pytest.raises(ValueError) as refused:
        hierarchy_backfill.backfill(repo, SLUG, "planner", apply=True)
    assert str(refused.value) == json.dumps(dry, sort_keys=True)
    assert repo.export_document(SLUG) == before


def test_apply_records_one_backfill_event_at_the_write_time(tmp_path, monkeypatch):
    repo = repository(tmp_path)
    before = repo.export_document(SLUG)
    monkeypatch.setattr(repo.domain, "now_ms", lambda: 1_790_000_000_000)
    hierarchy_backfill.backfill(repo, SLUG, "planner", apply=True)
    meta = store.SQLiteLedgerRepository(repo.path).export_document(SLUG)["_meta"]
    assert meta["updated_at"] == 1_790_000_000_000
    assert meta["events"] == [
        *before["_meta"]["events"],
        {
            "rev": before["_meta"]["rev"] + 1,
            "at": 1_790_000_000_000,
            "by": "planner",
            "kind": "backfilled",
            "target": "hierarchy",
        },
    ]


def test_apply_repairs_drifted_hierarchy_rows_with_one_reported_event(tmp_path):
    repo = repository(tmp_path)
    hierarchy_backfill.backfill(repo, SLUG, "planner", apply=True)
    with repo.connect() as connection, connection:
        node = connection.execute(
            "SELECT node_id FROM work_nodes WHERE ledger_slug=? ORDER BY node_id LIMIT 1", (SLUG,)
        ).fetchone()[0]
        connection.execute("DELETE FROM work_nodes WHERE ledger_slug=? AND node_id=?", (SLUG, node))
    events = len(repo.export_document(SLUG)["_meta"]["events"])
    repaired = hierarchy_backfill.backfill(repo, SLUG, "repairer", apply=True)
    assert repaired["repaired"]["missing_nodes"] == [node]
    assert repaired["repaired"]["drift"] == 1
    assert repaired["drift"]["drift"] == 0
    assert repaired["conflicts"] == []
    saved = repo.export_document(SLUG)["_meta"]["events"]
    assert len(saved) == events + 1
    assert (saved[-1]["by"], saved[-1]["kind"], saved[-1]["target"]) == ("repairer", "backfilled", "hierarchy")
    again = hierarchy_backfill.backfill(repo, SLUG, "repairer", apply=True)
    assert again["repaired"]["drift"] == 0
    assert len(repo.export_document(SLUG)["_meta"]["events"]) == events + 1


def test_the_applied_ledger_is_served_from_the_repository_cache(tmp_path, monkeypatch):
    repo = repository(tmp_path)
    hierarchy_backfill.backfill(repo, SLUG, "planner", apply=True)
    expected = store.SQLiteLedgerRepository(repo.path).export_document(SLUG)

    def reload(*args):
        raise AssertionError("the applied ledger was read back from its rows")

    monkeypatch.setattr(store, "read_rows", reload)
    assert repo.export_document(SLUG) == expected


def test_a_missing_database_or_ledger_is_named(tmp_path):
    repository(tmp_path)
    for repo in (
        store.SQLiteLedgerRepository(tmp_path / "empty" / store.DATABASE),
        store.SQLiteLedgerRepository(tmp_path / store.DATABASE),
    ):
        with pytest.raises(store.Missing) as missing:
            hierarchy_backfill.backfill(repo, "absent", "planner")
        assert missing.value.args == ("absent",)


def parse(*argv):
    return ledger.build_parser().parse_args(["--slug", SLUG, *argv])


def test_hierarchy_needs_its_backfill_action():
    with pytest.raises(SystemExit):
        parse("hierarchy")
    assert parse("hierarchy", "backfill").action == "backfill"


def test_command_refuses_a_remote_ledger_host(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTIHOOKS_DEPLOYMENT", "remote")
    monkeypatch.setattr(hierarchy_backfill, "repository", repository(tmp_path))
    with pytest.raises(SystemExit) as refused:
        hierarchy_backfill.run(parse("--as", "planner", "hierarchy", "backfill", "--apply"))
    assert refused.value.code == "hierarchy backfill must run on the local ledger host"


def test_dry_run_needs_no_name_and_prints_the_report_with_sorted_keys(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("AGENTIHOOKS_DEPLOYMENT", raising=False)
    monkeypatch.delenv("AGENTIHOOKS_AGENT_NAME", raising=False)
    repo = repository(tmp_path)
    monkeypatch.setattr(hierarchy_backfill, "repository", repo)
    monkeypatch.setattr("sys.argv", ["ledger", "--slug", SLUG, "hierarchy", "backfill"])
    ledger.main()
    assert capsys.readouterr().out == json.dumps(hierarchy_backfill.backfill(repo, SLUG, ""), sort_keys=True) + "\n"


def test_apply_without_any_name_is_refused(tmp_path, monkeypatch):
    monkeypatch.delenv("AGENTIHOOKS_DEPLOYMENT", raising=False)
    monkeypatch.delenv("AGENTIHOOKS_AGENT_NAME", raising=False)
    repo = repository(tmp_path)
    before = repo.export_document(SLUG)
    monkeypatch.setattr(hierarchy_backfill, "repository", repo)
    with pytest.raises(SystemExit) as refused:
        hierarchy_backfill.run(parse("hierarchy", "backfill", "--apply"))
    assert refused.value.code == "--as is required to apply the hierarchy backfill"
    assert repo.export_document(SLUG) == before


@pytest.mark.parametrize(("argv", "by"), [(("--as", "planner"), "planner"), ((), "engineer@session")])
def test_apply_records_the_named_actor_or_the_session_name(tmp_path, monkeypatch, capsys, argv, by):
    monkeypatch.delenv("AGENTIHOOKS_DEPLOYMENT", raising=False)
    monkeypatch.setenv("AGENTIHOOKS_AGENT_NAME", "engineer@session")
    repo = repository(tmp_path)
    monkeypatch.setattr(hierarchy_backfill, "repository", repo)
    hierarchy_backfill.run(parse(*argv, "hierarchy", "backfill", "--apply"))
    assert json.loads(capsys.readouterr().out)["applied"] is True
    assert repo.export_document(SLUG)["_meta"]["events"][-1]["by"] == by
