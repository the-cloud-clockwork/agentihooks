import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

import scripts.swarm_v2.architecture as architecture

ROOT = Path(__file__).resolve().parents[1]
RECORD = ROOT / "docs" / "swarm-v2" / "architecture.json"
MARKDOWN = ROOT / "docs" / "swarm-v2" / "decisions.md"
FIXTURES = Path(__file__).parent / "fixtures" / "swarm_v2" / "architecture"
DISPATCHER_REASON = "inserts another coding-task queue beside Swarm reconciliation controller (AD-05)"
BACKLOG_REASON = "a backlog must be bounded and carry one of transcripts, changed_content (AD-05)"


def _record(tmp_path):
    path = tmp_path / "architecture.json"
    shutil.copy(RECORD, path)
    return path


def _inventory(name="inventory.json"):
    return architecture.load_inventory(FIXTURES / name)


def _proposal(**fields):
    base = {
        "id": "p",
        "name": "New service",
        "kind": "service",
        "code_owner": "agentihooks",
        "state_owner": "agentihooks",
        "deployment_owner": "antoncore",
        "authoritative_state": "state",
    }
    return {**base, **fields}


def _single(*proposals, operation="op-1", base_revision=1):
    return {
        "schema": architecture.INVENTORY_SCHEMA,
        "operation": operation,
        "base_revision": base_revision,
        "proposals": list(proposals),
    }


def _run(*args):
    return subprocess.run(
        [sys.executable, "-m", "scripts.swarm_v2.architecture", *map(str, args)],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )


def test_committed_record_gives_every_component_one_owner_per_role():
    record = architecture.load_record(RECORD)
    assert architecture.check(record) == []
    assert architecture.authorities(record) == ["Swarm reconciliation controller"]
    assert all(architecture.missing_owner(record, c) is None for c in record["components"])
    assert [d["id"] for d in record["decisions"]] == [f"AD-0{n}" for n in range(1, 8)]


def test_committed_decisions_markdown_matches_the_record():
    assert MARKDOWN.read_text() == architecture.render(architecture.load_record(RECORD))


def test_fixture_review_rejects_the_dispatcher_and_accepts_the_embedding_backlog():
    result = architecture.review(architecture.load_record(RECORD), _inventory())
    assert result == {
        "operation": "fixture-inventory-1",
        "revision": 1,
        "accepted": ["embedding-backlog"],
        "rejected": [{"id": "duplicate-dispatcher", "name": "Kubernetes task dispatcher", "reason": DISPATCHER_REASON}],
        "unresolved": [],
        "unowned": [],
        "measurements": {"architecture_unowned_components": 0},
    }


def test_recording_the_fixture_adds_only_the_backlog_and_keeps_the_rejection(tmp_path):
    path = _record(tmp_path)
    before = architecture.load_record(path)
    result = architecture.apply_inventory(path, _inventory())
    after = architecture.load_record(path)
    assert result["accepted"] == ["embedding-backlog"]
    assert after["revision"] == 2
    assert after["components"][:-1] == before["components"]
    assert after["components"][-1] == {
        "name": "Brain arc embedding backlog",
        "kind": "backlog",
        "carries": "changed_content",
        "bounded": True,
        "launches_agents": False,
        "code_owner": "agentibrain-kernel",
        "state_owner": "agentibrain-kernel",
        "deployment_owner": "antoncore",
        "authoritative_state": "Changed arc ids awaiting embedding",
        "proposal": "embedding-backlog",
        "added_in": 2,
    }
    assert after["rejected"] == [
        {
            "id": "duplicate-dispatcher",
            "name": "Kubernetes task dispatcher",
            "reason": DISPATCHER_REASON,
            "operation": "fixture-inventory-1",
            "revision": 2,
        }
    ]
    assert after["unresolved"] == []
    assert after["operations"] == [
        {"id": "fixture-inventory-1", "revision": 2, "sha256": architecture.digest(_inventory()), "result": result}
    ]
    assert architecture.check(after) == []
    assert architecture.authorities(after) == ["Swarm reconciliation controller"]


def test_second_independent_fixture_is_judged_without_state_from_the_first(tmp_path):
    first_dir, second_dir = tmp_path / "first", tmp_path / "second"
    first_dir.mkdir()
    second_dir.mkdir()
    architecture.apply_inventory(_record(first_dir), _inventory())
    second = _record(second_dir)
    result = architecture.apply_inventory(second, _inventory("inventory-second.json"))
    assert result["accepted"] == ["session-exporter"]
    assert result["rejected"] == [
        {"id": "backlog-that-dispatches", "name": "Review retry backlog", "reason": DISPATCHER_REASON}
    ]
    after = architecture.load_record(second)
    assert [o["id"] for o in after["operations"]] == ["fixture-inventory-2"]
    assert "Brain arc embedding backlog" not in [c["name"] for c in after["components"]]
    assert architecture.check(after) == []


def test_render_lists_components_decisions_and_open_items(tmp_path):
    path = _record(tmp_path)
    architecture.apply_inventory(path, _inventory())
    text = architecture.render(architecture.load_record(path))
    assert text.startswith("# Swarm v2 architecture decisions\n\nPackage SV2-FND-02, record revision 2.")
    assert (
        "| Brain arc embedding backlog | backlog | agentibrain-kernel | agentibrain-kernel | antoncore |"
        " Changed arc ids awaiting embedding |\n" in text
    )
    assert "\n## AD-05: One coding-task authority; backlogs never dispatch work\n\nStatus: accepted.\n" in text
    assert "\n- A second work-stealing queue for coding tasks: Two claim authorities break generation fencing" in text
    assert "\n## Worker image exclusions\n\n- brain database\n- brain tick stack\n" in text
    assert "\nOperator architecture changes: none.\n" in text
    assert "\n## Unresolved decisions\n\nNone.\n" in text
    assert text.endswith(
        f"\n## Rejected proposals\n\n- Kubernetes task dispatcher (`duplicate-dispatcher`, revision 2): {DISPATCHER_REASON}\n"
    )


def test_render_names_operator_changes_and_unresolved_entries():
    record = architecture.load_record(RECORD)
    record["operator_changes"] = ["a", "b"]
    record["unresolved"] = [{"id": "x", "name": "X", "reason": "why", "revision": 3}]
    text = architecture.render(record)
    assert "\nOperator architecture changes: a, b.\n" in text
    assert "\n## Unresolved decisions\n\n- X (`x`, revision 3): why\n\n## Rejected proposals\n\nNone.\n" in text


def test_cli_review_prints_the_fixture_verdicts():
    proc = _run("review", "--record", RECORD, "--inventory", FIXTURES / "inventory.json")
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout)["accepted"] == ["embedding-backlog"]


def test_cli_record_writes_the_record_and_its_markdown(tmp_path):
    path = _record(tmp_path)
    markdown = tmp_path / "decisions.md"
    proc = _run("record", "--record", path, "--inventory", FIXTURES / "inventory.json", "--markdown", markdown)
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout)["rejected"][0]["id"] == "duplicate-dispatcher"
    assert markdown.read_text() == architecture.render(architecture.load_record(path))


def test_cli_check_passes_on_the_committed_record():
    proc = _run("check", "--record", RECORD)
    assert (proc.returncode, proc.stdout) == (0, "ok\n")


@pytest.mark.parametrize(
    ("fields", "reason"),
    [
        ({"kind": "queue"}, "unknown kind 'queue'"),
        ({"kind": None}, "unknown kind None"),
        ({"code_owner": ""}, "code_owner must name exactly one owner from the record"),
        ({"state_owner": ["agentihooks", "antoncore"]}, "state_owner must name exactly one owner from the record"),
        ({"deployment_owner": "operator laptop"}, "deployment_owner must name exactly one owner from the record"),
        (
            {"kind": "worker_component", "name": "brain tick stack"},
            "the worker image excludes brain tick stack (AD-06)",
        ),
        (
            {"kind": "worker_component", "name": "transcript database"},
            "the worker image excludes transcript database (AD-06)",
        ),
        ({"kind": "dispatcher"}, DISPATCHER_REASON),
        ({"carries": "coding_tasks"}, DISPATCHER_REASON),
        ({"launches_agents": True}, DISPATCHER_REASON),
        ({"kind": "backlog", "carries": "transcripts", "bounded": False}, BACKLOG_REASON),
        ({"kind": "backlog", "carries": "transcripts", "bounded": 1}, BACKLOG_REASON),
        ({"kind": "backlog", "carries": "transcripts"}, BACKLOG_REASON),
        ({"kind": "backlog", "carries": "pull_requests", "bounded": True}, BACKLOG_REASON),
    ],
)
def test_review_rejects_proposals_outside_the_frozen_architecture(fields, reason):
    proposal = _proposal(**fields)
    result = architecture.review(architecture.load_record(RECORD), _single(proposal))
    assert result["accepted"] == []
    assert result["unresolved"] == []
    assert result["rejected"] == [{"id": "p", "name": proposal["name"], "reason": reason}]


@pytest.mark.parametrize(
    "fields",
    [
        {},
        {"launches_agents": False},
        {"kind": "worker_component", "name": "Worker session exporter"},
        {"kind": "backlog", "carries": "transcripts", "bounded": True},
        {"kind": "backlog", "carries": "changed_content", "bounded": True, "launches_agents": False},
    ],
)
def test_review_accepts_owned_proposals_inside_the_architecture(fields):
    result = architecture.review(architecture.load_record(RECORD), _single(_proposal(**fields)))
    assert (result["accepted"], result["rejected"], result["unresolved"]) == (["p"], [], [])


def test_unowned_proposals_are_counted():
    inventory = _single(
        _proposal(id="a", name="A", code_owner=None),
        _proposal(id="b", name="B"),
        _proposal(id="c", name="C", state_owner="x"),
    )
    result = architecture.review(architecture.load_record(RECORD), inventory)
    assert result["unowned"] == ["A", "C"]
    assert result["measurements"] == {"architecture_unowned_components": 2}


def test_an_unowned_recorded_component_is_counted_and_fails_check():
    record = architecture.load_record(RECORD)
    record["components"][1]["deployment_owner"] = None
    assert architecture.review(record, _single())["unowned"] == ["Local runtime adapter"]
    assert architecture.check(record) == [
        "Local runtime adapter: deployment_owner must name exactly one owner from the record"
    ]


def test_check_reports_every_broken_rule():
    record = architecture.load_record(RECORD)
    record["components"].append(_proposal(name="Operator interface"))
    record["components"].append(_proposal(name="brain database", kind="worker_component"))
    record["components"].append(_proposal(name="Second dispatcher", kind="dispatcher"))
    assert architecture.check(record) == [
        "Operator interface is recorded more than once",
        "brain database is excluded from the worker image",
        "expected one coding-task authority, found 2",
    ]
    record["components"] = [c for c in record["components"] if not architecture.dispatches(c)]
    assert architecture.check(record)[-1] == "expected one coding-task authority, found 0"


def test_cli_check_fails_on_a_broken_record(tmp_path):
    path = _record(tmp_path)
    data = architecture.load_record(path)
    data["components"].append(_proposal(name="Second dispatcher", kind="dispatcher"))
    path.write_text(json.dumps(data))
    proc = _run("check", "--record", path)
    assert (proc.returncode, proc.stdout) == (1, "expected one coding-task authority, found 2\n")


def test_an_operator_change_in_the_record_admits_that_dispatcher_only(tmp_path):
    path = _record(tmp_path)
    data = architecture.load_record(path)
    data["operator_changes"] = ["duplicate-dispatcher"]
    path.write_text(json.dumps(data))
    inventory = _inventory()
    inventory["proposals"].append(_proposal(id="other", name="Other dispatcher", kind="dispatcher"))
    result = architecture.apply_inventory(path, inventory)
    assert result["accepted"] == ["duplicate-dispatcher", "embedding-backlog"]
    assert result["rejected"] == [{"id": "other", "name": "Other dispatcher", "reason": DISPATCHER_REASON}]
    after = architecture.load_record(path)
    assert architecture.authorities(after) == ["Swarm reconciliation controller"]
    assert architecture.check(after) == []


def test_a_proposal_cannot_approve_itself():
    proposal = _proposal(kind="dispatcher", operator_changes=["p"], approved_by="operator")
    result = architecture.review(architecture.load_record(RECORD), _single(proposal))
    assert result["rejected"] == [{"id": "p", "name": "New service", "reason": DISPATCHER_REASON}]


def test_a_stale_revision_is_refused_and_the_record_is_unchanged(tmp_path):
    path = _record(tmp_path)
    architecture.apply_inventory(path, _inventory())
    before = path.read_bytes()
    with pytest.raises(architecture.ArchitectureError) as error:
        architecture.apply_inventory(path, _inventory("inventory-second.json"))
    assert str(error.value) == "inventory is based on revision 1; the record is at revision 2"
    assert path.read_bytes() == before


def test_a_newer_base_revision_is_refused(tmp_path):
    path = _record(tmp_path)
    with pytest.raises(
        architecture.ArchitectureError, match="^inventory is based on revision 5; the record is at revision 1$"
    ):
        architecture.apply_inventory(path, _single(_proposal(), base_revision=5))


def test_documents_with_the_wrong_schema_are_refused(tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"schema": "other", "proposals": []}))
    with pytest.raises(architecture.ArchitectureError) as error:
        architecture.load_inventory(bad)
    assert str(error.value) == f"{bad} is not a swarm-v2-design-inventory/1 document"
    with pytest.raises(architecture.ArchitectureError) as error:
        architecture.load_record(bad)
    assert str(error.value) == f"{bad} is not a swarm-v2-architecture/1 document"
    bad.write_text("{}")
    with pytest.raises(architecture.ArchitectureError, match="is not a swarm-v2-architecture/1 document$"):
        architecture.load_record(bad)


def test_an_inventory_with_repeated_proposal_ids_is_refused(tmp_path):
    path = tmp_path / "inventory.json"
    path.write_text(json.dumps(_single(_proposal(name="A"), _proposal(name="B"))))
    with pytest.raises(architecture.ArchitectureError) as error:
        architecture.load_inventory(path)
    assert str(error.value) == "proposal ids must be unique"


def test_cli_reports_a_refusal_without_a_traceback(tmp_path):
    path = _record(tmp_path)
    architecture.apply_inventory(path, _inventory())
    before = path.read_bytes()
    proc = _run(
        "record", "--record", path, "--inventory", FIXTURES / "inventory-second.json", "--markdown", tmp_path / "d.md"
    )
    assert proc.returncode == 2
    assert proc.stdout == ""
    assert proc.stderr == "error: inventory is based on revision 1; the record is at revision 2\n"
    assert path.read_bytes() == before
    assert not (tmp_path / "d.md").exists()


def test_conflicting_proposals_become_unresolved_while_unrelated_ones_are_recorded(tmp_path):
    path = _record(tmp_path)
    inventory = _single(
        _proposal(id="a", name="Shared catalog", deployment_owner="antoncore"),
        _proposal(id="b", name="Shared catalog", deployment_owner="personal installation"),
        _proposal(id="c", name="Operator interface"),
        _proposal(id="d", name="Unrelated service"),
    )
    result = architecture.apply_inventory(path, inventory)
    assert result["accepted"] == ["d"]
    assert result["unresolved"] == [
        {"id": "a", "name": "Shared catalog", "reason": "conflicting proposals for Shared catalog"},
        {"id": "b", "name": "Shared catalog", "reason": "conflicting proposals for Shared catalog"},
        {
            "id": "c",
            "name": "Operator interface",
            "reason": "Operator interface is already recorded; changing it needs an operator architecture change",
        },
    ]
    after = architecture.load_record(path)
    assert [u["id"] for u in after["unresolved"]] == ["a", "b", "c"]
    assert {u["operation"] for u in after["unresolved"]} == {"op-1"}
    assert {u["revision"] for u in after["unresolved"]} == {2}
    assert after["components"][-1]["name"] == "Unrelated service"
    assert [c["name"] for c in after["components"]].count("Operator interface") == 1


def test_a_replayed_operation_has_no_second_effect(tmp_path):
    path = _record(tmp_path)
    first = architecture.apply_inventory(path, _inventory())
    before = path.read_bytes()
    assert architecture.apply_inventory(path, _inventory()) == first
    assert path.read_bytes() == before


def test_a_replay_with_changed_content_is_refused(tmp_path):
    path = _record(tmp_path)
    architecture.apply_inventory(path, _inventory())
    before = path.read_bytes()
    changed = _inventory()
    changed["base_revision"] = 2
    changed["proposals"].pop()
    with pytest.raises(architecture.ArchitectureError) as error:
        architecture.apply_inventory(path, changed)
    assert str(error.value) == "operation fixture-inventory-1 was already recorded with different content"
    assert path.read_bytes() == before


def test_digest_ignores_key_order():
    assert architecture.digest({"a": 1, "b": 2}) == architecture.digest({"b": 2, "a": 1})
    assert architecture.digest({"a": 1}) == "f9d86028c6e0d64e225186f96acb69338b2c59764df79162107f5c4bb34d1310"


def test_an_interrupted_write_keeps_the_previous_record(tmp_path, monkeypatch):
    tmp_path = tmp_path / "work"
    tmp_path.mkdir()
    path = _record(tmp_path)
    before = path.read_bytes()

    def fail(*_):
        raise OSError("disk gone")

    monkeypatch.setattr(architecture.os, "replace", fail)
    with pytest.raises(OSError, match="disk gone"):
        architecture.apply_inventory(path, _inventory())
    assert path.read_bytes() == before
    assert sorted(p.name for p in tmp_path.iterdir()) == ["architecture.json"]
    monkeypatch.undo()
    assert architecture.apply_inventory(path, _inventory())["accepted"] == ["embedding-backlog"]


def test_a_written_record_is_indented_json_with_a_trailing_newline(tmp_path):
    path = _record(tmp_path)
    architecture.apply_inventory(path, _inventory())
    text = path.read_text()
    assert text == json.dumps(json.loads(text), indent=2) + "\n"


def test_rollback_restores_the_earlier_revision_and_keeps_every_rejection(tmp_path):
    path = _record(tmp_path)
    architecture.apply_inventory(path, _inventory())
    base = architecture.load_record(RECORD)["components"]
    result = architecture.rollback(path, 1, "rollback-1")
    after = architecture.load_record(path)
    assert result == {"operation": "rollback-1", "revision": 2, "rolled_back": ["Brain arc embedding backlog"]}
    assert after["revision"] == 3
    assert after["components"] == base
    assert after["rejected"] == [
        {
            "id": "duplicate-dispatcher",
            "name": "Kubernetes task dispatcher",
            "reason": DISPATCHER_REASON,
            "operation": "fixture-inventory-1",
            "revision": 2,
        },
        {
            "id": "embedding-backlog",
            "name": "Brain arc embedding backlog",
            "reason": "rolled back to revision 1",
            "operation": "rollback-1",
            "revision": 3,
        },
    ]
    assert after["operations"][-1] == {
        "id": "rollback-1",
        "revision": 3,
        "sha256": architecture.digest({"rollback_to": 1}),
        "result": result,
    }
    assert architecture.check(after) == []


def test_rollback_keeps_components_up_to_the_target_revision(tmp_path):
    path = _record(tmp_path)
    architecture.apply_inventory(path, _inventory())
    architecture.apply_inventory(path, _single(_proposal(name="Later service"), operation="op-2", base_revision=2))
    result = architecture.rollback(path, 2, "rollback-2")
    assert result["rolled_back"] == ["Later service"]
    assert architecture.load_record(path)["components"][-1]["name"] == "Brain arc embedding backlog"


def test_rollback_replay_and_refusals(tmp_path):
    path = _record(tmp_path)
    with pytest.raises(architecture.ArchitectureError) as error:
        architecture.rollback(path, 1, "r")
    assert str(error.value) == "rollback target 1 is not an earlier revision of 1"
    architecture.apply_inventory(path, _inventory())
    for target in (0, 2):
        with pytest.raises(architecture.ArchitectureError) as error:
            architecture.rollback(path, target, "r")
        assert str(error.value) == f"rollback target {target} is not an earlier revision of 2"
    first = architecture.rollback(path, 1, "r")
    before = path.read_bytes()
    assert architecture.rollback(path, 1, "r") == first
    assert path.read_bytes() == before
    with pytest.raises(
        architecture.ArchitectureError, match="^operation r was already recorded with different content$"
    ):
        architecture.rollback(path, 2, "r")


def test_cli_rollback_writes_the_record_and_its_markdown(tmp_path):
    path = _record(tmp_path)
    architecture.apply_inventory(path, _inventory())
    markdown = tmp_path / "decisions.md"
    proc = _run("rollback", "--record", path, "--to", 1, "--operation", "r", "--markdown", markdown)
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout)["rolled_back"] == ["Brain arc embedding backlog"]
    assert markdown.read_text() == architecture.render(architecture.load_record(path))


def test_cli_render_writes_markdown_only(tmp_path):
    path = _record(tmp_path)
    before = path.read_bytes()
    markdown = tmp_path / "decisions.md"
    proc = _run("render", "--record", path, "--markdown", markdown)
    assert (proc.returncode, proc.stdout) == (0, "")
    assert markdown.read_text() == architecture.render(architecture.load_record(path))
    assert path.read_bytes() == before


def test_main_defaults_point_at_the_committed_record(monkeypatch, capsys):
    monkeypatch.chdir(ROOT)
    assert architecture.main(["check"]) == 0
    assert capsys.readouterr().out == "ok\n"
    assert (architecture.RECORD, architecture.MARKDOWN) == (
        "docs/swarm-v2/architecture.json",
        "docs/swarm-v2/decisions.md",
    )
