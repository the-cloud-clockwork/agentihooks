import hashlib
import json
from pathlib import Path

import pytest

from hooks.memory import retention as policy
from scripts.swarm_v2.checkpoints import retention
from tests import sv2_ret01_cases as cases

pytestmark = pytest.mark.unit
EVIDENCE = Path(__file__).parents[1] / "evidence" / "SV2-RET-01"
ELIGIBLE = [
    "ckpt-live-1",
    "ckpt-live-3",
    "ckpt-old",
    "log-old",
    "old-native",
    "old-transcript",
    "personal-preview",
]
PROTECTED = {"active_restore": 3, "evidence_hold": 1, "latest_checkpoint": 3, "not_expired": 5, "referenced": 1}


def _collector(root, destructive=True):
    inventory, holds, policies, now = cases.world()
    return (
        inventory,
        holds,
        retention.Collector(
            inventory, holds, policies, retention.FileJournal(root / "journal.jsonl"), lambda: now, destructive
        ),
    )


def test_plan_protects_restore_evidence_latest_and_referenced_objects(tmp_path):
    _, _, collector = _collector(tmp_path)
    report = collector.plan()
    assert sorted(report.eligible()) == ELIGIBLE
    assert report.metrics() == {"retention_protected_objects": PROTECTED}
    assert report.decisions["old-shared"] == "referenced"
    assert report.decisions["live-shared"] == "latest_checkpoint"
    assert report.decisions["ckpt-restore"] == "active_restore"
    assert report.decisions["evidence-held"] == "evidence_hold"
    assert report.decisions["preview-kept"] == "not_expired"
    assert report.decisions["personal-transcript"] == "not_expired"
    assert not (tmp_path / "journal.jsonl").exists()


def test_collect_deletes_manifests_first_and_only_eligible_objects(tmp_path):
    inventory, _, collector = _collector(tmp_path)
    report = collector.collect("run")
    assert inventory.removals == [
        "ckpt-live-1",
        "ckpt-live-3",
        "ckpt-old",
        "log-old",
        "old-native",
        "old-transcript",
        "personal-preview",
    ]
    assert list(report.deleted) == inventory.removals
    assert collector.collect("run").deleted == report.deleted
    assert len(inventory.removals) == 7


def test_corpus_without_declared_policy_keeps_everything(tmp_path):
    doc = cases.fixture()
    doc["policies"] = [item for item in doc["policies"] if item["corpus"] == "swarm"]
    inventory, holds, policies, now = cases.world(doc)
    report = retention.Collector(inventory, holds, policies, retention.FileJournal(tmp_path / "j"), lambda: now).plan()
    assert {k: v for k, v in report.decisions.items() if k.startswith("personal-")} == {
        "personal-preview": "no_policy",
        "personal-transcript": "no_policy",
        "personal-vector": "no_policy",
    }


def test_delete_refuses_protected_object_without_mutation(tmp_path):
    inventory, _, collector = _collector(tmp_path)
    before = dict(inventory.stored)
    with pytest.raises(retention.RetentionRefused) as raised:
        collector.delete("restore-native")
    assert str(raised.value) == "object restore-native is not eligible for deletion: active_restore"
    assert inventory.stored == before
    assert inventory.removals == []


def test_delete_refuses_eligible_object_when_destructive_collection_is_off(tmp_path):
    inventory, _, collector = _collector(tmp_path, destructive=False)
    with pytest.raises(retention.RetentionRefused) as raised:
        collector.delete("log-old")
    assert str(raised.value) == "destructive collection is off; object log-old is kept"
    assert inventory.removals == []


def test_delete_removes_an_eligible_object(tmp_path):
    inventory, _, collector = _collector(tmp_path)
    collector.delete("log-old")
    assert inventory.removals == ["log-old"]


def test_restore_registered_during_collection_protects_its_objects(tmp_path):
    inventory, holds, collector = _collector(tmp_path)
    original = inventory.remove

    def remove(object_id):
        original(object_id)
        holds.restores.add("ckpt-old")

    inventory.remove = remove
    report = collector.collect("run")
    assert inventory.removals == ["ckpt-live-1", "ckpt-live-3", "log-old", "personal-preview"]
    assert report.skipped == {
        "ckpt-old": "active_restore",
        "old-native": "active_restore",
        "old-transcript": "active_restore",
    }


def test_evidence_hold_registered_during_collection_protects_the_object(tmp_path):
    inventory, holds, collector = _collector(tmp_path)
    original = inventory.remove

    def remove(object_id):
        original(object_id)
        holds.held.add("personal-preview")

    inventory.remove = remove
    report = collector.collect("run")
    assert report.skipped == {"personal-preview": "evidence_hold"}
    assert "personal-preview" in inventory.stored


def test_object_gone_before_its_turn_is_journaled_absent(tmp_path):
    inventory, _, collector = _collector(tmp_path, destructive=False)
    collector.collect("run")
    del inventory.stored["log-old"]
    resumed = retention.Collector(
        inventory, collector.holds, collector.policies, collector.journal, collector.clock, True
    ).collect("run")
    assert "log-old" not in resumed.deleted
    ops = [(e["op"], e.get("object_id")) for e in collector.journal.entries("run")]
    assert ("absent", "log-old") in ops


def test_dry_run_journals_plan_and_deletes_nothing(tmp_path):
    inventory, _, collector = _collector(tmp_path, destructive=False)
    report = collector.collect("run")
    entries = collector.journal.entries("run")
    assert inventory.removals == []
    assert report.destructive is False
    assert report.deleted == ()
    assert [entry["op"] for entry in entries] == ["planned"]
    assert entries[0]["eligible"] == [
        "ckpt-live-1",
        "ckpt-live-3",
        "ckpt-old",
        "log-old",
        "old-native",
        "old-transcript",
        "personal-preview",
    ]
    assert entries[0]["protected"] == PROTECTED
    assert entries[0]["policies"] == {"swarm": "swarm-retention-1", "personal": "personal-retention-1"}
    assert entries[0]["at"] == 100000000.0


def test_destructive_switch_reads_environment():
    assert retention.destructive_enabled({}) is True
    assert retention.destructive_enabled({"SWARM_RETENTION_DESTRUCTIVE": "on"}) is True
    assert retention.destructive_enabled({"SWARM_RETENTION_DESTRUCTIVE": "off"}) is False


@pytest.mark.parametrize(("switch", "removed"), [("off", 0), ("on", 7)])
def test_default_collector_follows_the_environment_switch(tmp_path, monkeypatch, switch, removed):
    monkeypatch.setenv("SWARM_RETENTION_DESTRUCTIVE", switch)
    inventory, holds, policies, now = cases.world()
    collector = retention.Collector(inventory, holds, policies, retention.FileJournal(tmp_path / "j"), lambda: now)
    report = collector.collect("run")
    assert collector.destructive is (switch == "on")
    assert len(inventory.removals) == removed
    assert len(report.deleted) == removed


def test_checkpoint_completed_during_collection_protects_what_it_references(tmp_path):
    inventory, _, collector = _collector(tmp_path)
    original = inventory.remove

    def remove(object_id):
        original(object_id)
        if object_id == "ckpt-live-1":
            inventory.stored["ckpt-new"] = retention.StoredObject("ckpt-new", "swarm", "native_resume", 100000000.0)
            inventory.manifests.append(retention.Checkpoint("ckpt-new", "t-done", 3, True, ("old-native", "log-old")))

    inventory.remove = remove
    report = collector.collect("run")
    assert report.skipped == {"log-old": "referenced", "old-native": "referenced"}
    assert inventory.removals == ["ckpt-live-1", "ckpt-live-3", "ckpt-old", "old-transcript", "personal-preview"]


def test_restore_on_a_checkpoint_added_during_collection_holds_its_objects(tmp_path):
    inventory, holds, collector = _collector(tmp_path)
    original = inventory.remove

    def remove(object_id):
        original(object_id)
        if object_id == "ckpt-live-1":
            inventory.stored["ckpt-new"] = retention.StoredObject("ckpt-new", "swarm", "native_resume", 0.0)
            inventory.manifests.append(retention.Checkpoint("ckpt-new", "t-done", 3, False, ("personal-preview",)))
            holds.restores.add("ckpt-new")

    inventory.remove = remove
    report = collector.collect("run")
    assert report.skipped == {"personal-preview": "active_restore"}


def test_object_removed_out_of_band_during_collection_is_journaled_absent(tmp_path):
    inventory, _, collector = _collector(tmp_path)
    original = inventory.remove

    def remove(object_id):
        original(object_id)
        inventory.stored.pop("log-old", None)

    inventory.remove = remove
    report = collector.collect("run")
    assert "log-old" not in report.deleted
    assert [e["op"] for e in collector.journal.entries("run") if e.get("object_id") == "log-old"] == ["absent"]


def test_restore_pin_outranks_a_latest_checkpoint_sharing_an_object():
    checkpoints = [
        retention.Checkpoint("r", "a", 1, True, ("shared", "own")),
        retention.Checkpoint("l", "b", 1, True, ("shared",)),
    ]
    assert retention._pinned(checkpoints, frozenset({"r"}), {"l"}) == {
        "r": "active_restore",
        "shared": "active_restore",
        "own": "active_restore",
        "l": "latest_checkpoint",
    }


def test_file_journal_appends_to_an_empty_file_without_a_blank_line(tmp_path):
    path = tmp_path / "journal.jsonl"
    path.write_bytes(b"")
    retention.FileJournal(path).append("run", {"op": "planned"})
    assert path.read_bytes() == b'{"op": "planned", "run_id": "run"}\n'


def test_file_journal_ignores_and_closes_a_torn_line(tmp_path):
    path = tmp_path / "journal.jsonl"
    journal = retention.FileJournal(path)
    journal.append("run", {"op": "planned", "eligible": []})
    with path.open("a", encoding="utf-8") as handle:
        handle.write('{"op": "dele')
    journal.append("run", {"op": "deleted", "object_id": "x"})
    journal.append("other", {"op": "planned", "eligible": []})
    assert journal.entries("run") == [
        {"op": "planned", "eligible": [], "run_id": "run"},
        {"op": "deleted", "object_id": "x", "run_id": "run"},
    ]
    assert retention.FileJournal(tmp_path / "missing").entries("run") == []


def test_latest_complete_checkpoint_ignores_incomplete_and_inactive_tasks():
    checkpoints = [
        retention.Checkpoint("a1", "a", 1, True),
        retention.Checkpoint("a2", "a", 2, True),
        retention.Checkpoint("a3", "a", 3, False),
        retention.Checkpoint("b1", "b", 1, True),
    ]
    assert retention._latest(checkpoints, frozenset({"a"})) == {"a2"}
    assert retention._latest(list(reversed(checkpoints)), frozenset({"a"})) == {"a2"}


def test_unknown_data_class_is_kept(tmp_path):
    inventory, _, collector = _collector(tmp_path)
    inventory.stored["odd"] = retention.StoredObject("odd", "swarm", "thumbnail", 0.0)
    assert collector.plan().decisions["odd"] == "unknown_class"


@pytest.mark.parametrize(
    ("doc", "message"),
    [
        ([], None),
        ("swarm", "retention policies are a list with one policy per corpus"),
        ([{"corpus": "swarm"}], "a retention policy names exactly its corpus, version and max age per class"),
        (
            [{"corpus": "team", "version": "1", "max_age_seconds": {}}],
            "a retention policy corpus is personal or swarm",
        ),
        ([{"corpus": "swarm", "version": "", "max_age_seconds": {}}], "a retention policy carries a version"),
        (
            [{"corpus": "swarm", "version": "1", "max_age_seconds": {"preview": 1}}],
            "a retention policy states a max age for every data class",
        ),
        (
            [{"corpus": "swarm", "version": "1", "max_age_seconds": dict.fromkeys(policy.CLASSES, 0)}],
            "a max age is a positive number of seconds, or null to keep the class",
        ),
        (
            [{"corpus": "swarm", "version": "1", "max_age_seconds": dict.fromkeys(policy.CLASSES, True)}],
            "a max age is a positive number of seconds, or null to keep the class",
        ),
        (
            [{"corpus": "swarm", "version": v, "max_age_seconds": dict.fromkeys(policy.CLASSES)} for v in "12"],
            "each corpus declares at most one retention policy",
        ),
    ],
)
def test_policy_parse_requires_an_explicit_complete_policy(doc, message):
    if message is None:
        assert policy.Policies.parse(doc).declared == ()
        return
    with pytest.raises(policy.PolicyError) as raised:
        policy.Policies.parse(doc)
    assert str(raised.value) == message


def test_policy_expiry_boundary_and_retain_until_deleted():
    parsed = policy.Policies.parse(cases.fixture()["policies"]).get("swarm")
    assert parsed.expired("preview", 0.0, 604800.0) is True
    assert parsed.expired("preview", 0.0, 604799.0) is False
    assert parsed.expired("preview", 0.0, 10**9, "retain-until-deleted") is False
    personal = policy.Policies.parse(cases.fixture()["policies"]).get("personal")
    assert personal.expired("raw_evidence", 0.0, 10**12) is False
    assert policy.Policies.parse([]).get("swarm") is None


def test_file_journal_writes_sorted_keys_one_entry_per_line(tmp_path):
    path = tmp_path / "journal.jsonl"
    journal = retention.FileJournal(path)
    journal.append("r", {"op": "deleted", "object_id": "x"})
    journal.append("r", {"op": "deleted", "object_id": "y"})
    assert path.read_bytes() == (
        b'{"object_id": "x", "op": "deleted", "run_id": "r"}\n{"object_id": "y", "op": "deleted", "run_id": "r"}\n'
    )


def test_reports_carry_their_run_and_mode(tmp_path):
    _, _, collector = _collector(tmp_path)
    assert (collector.plan().run_id, collector.plan().destructive) == ("", False)
    assert collector.plan("r").run_id == "r"
    report = collector.collect("run")
    assert (report.run_id, report.destructive) == ("run", True)
    (tmp_path / "dry").mkdir()
    _, _, dry = _collector(tmp_path / "dry", destructive=False)
    dry_report = dry.collect("dry")
    assert (dry_report.run_id, dry_report.destructive) == ("dry", False)


def test_delete_refuses_an_unknown_object_as_absent(tmp_path):
    _, _, collector = _collector(tmp_path)
    with pytest.raises(retention.RetentionRefused) as raised:
        collector.delete("nope")
    assert str(raised.value) == "object nope is not eligible for deletion: absent"


def test_objects_of_a_checkpoint_whose_manifest_is_gone_are_not_referenced(tmp_path):
    inventory, _, collector = _collector(tmp_path)
    del inventory.stored["ckpt-old"]
    inventory.checkpoints = lambda: list(inventory.manifests)
    decisions = collector.plan().decisions
    assert (decisions["old-native"], decisions["old-transcript"]) == ("eligible", "eligible")


def test_manifests_order_before_other_objects():
    checkpoints = [retention.Checkpoint("m", "t", 1, True)]
    assert retention._ordered(["b", "a", "m"], checkpoints) == ["m", "a", "b"]


def test_latest_keeps_the_first_of_equal_sequences():
    checkpoints = [retention.Checkpoint("x", "a", 1, True), retention.Checkpoint("y", "a", 1, True)]
    assert retention._latest(checkpoints, frozenset({"a"})) == {"x"}


def _document(path):
    doc = cases.fixture()
    for item in doc["objects"]:
        item["created_at"] = doc["now"] - item.pop("age_days") * cases.DAY
    path.write_text(json.dumps({key: doc[key] for key in ("policies", "objects", "checkpoints", "holds")}))
    return float(doc["now"])


def test_cli_plan_reports_without_deleting(tmp_path, capsys):
    path = tmp_path / "store.json"
    now = _document(path)
    before = path.read_bytes()
    code = retention.main(["plan", str(path), "--journal", str(tmp_path / "j")], clock=lambda: now)
    summary = json.loads(capsys.readouterr().out)
    assert code == 0
    assert path.read_bytes() == before
    assert not (tmp_path / "j").exists()
    assert (summary["run_id"], summary["destructive"], summary["deleted"]) == ("", False, [])
    assert sorted(k for k, v in summary["decisions"].items() if v == "eligible") == ELIGIBLE
    assert summary["retention_protected_objects"] == PROTECTED


def test_cli_collect_removes_eligible_objects_from_the_document(tmp_path, capsys, monkeypatch):
    monkeypatch.delenv("SWARM_RETENTION_DESTRUCTIVE", raising=False)
    path = tmp_path / "store.json"
    now = _document(path)
    argv = ["collect", str(path), "--journal", str(tmp_path / "j"), "--run-id", "r1"]
    assert retention.main(argv, clock=lambda: now) == 0
    summary = json.loads(capsys.readouterr().out)
    remaining = {item["object_id"] for item in json.loads(path.read_bytes())["objects"]}
    assert (summary["run_id"], summary["destructive"], summary["skipped"]) == ("r1", True, {})
    assert sorted(summary["deleted"]) == ELIGIBLE
    assert remaining.isdisjoint(ELIGIBLE)
    assert len(remaining) == 13
    store = retention.DocumentInventory(path)
    assert store.restoring() == frozenset({"ckpt-restore"})
    assert store.evidence() == frozenset({"evidence-held"})
    assert store.active_tasks() == frozenset({"t-live", "t-restore"})


def test_cli_requires_a_journal(tmp_path, capsys):
    with pytest.raises(SystemExit) as raised:
        retention.main(["plan", str(tmp_path / "store.json")])
    assert raised.value.code == 2


@pytest.mark.parametrize("case", ["A", "B", "C"])
def test_package_cases_match_committed_evidence(tmp_path, case):
    observed = cases.run_case(case, tmp_path)
    path = EVIDENCE / f"{case.lower()}-result.json"
    expected = json.loads(path.read_text()) if path.exists() else None
    assert observed == expected, json.dumps(observed, sort_keys=True)


def test_package_record_and_manifest_match_the_fixture_inputs():
    record = json.loads((EVIDENCE / "result.json").read_text())
    manifest = json.loads((EVIDENCE / "manifest.json").read_text())
    digest = hashlib.sha256(cases.FIXTURE.read_bytes()).hexdigest()
    assert manifest["input_sha256"] == {cases.FIXTURE.name: digest}
    assert manifest["fixture_inputs"] == record["fixture_inputs"] == cases.FIXTURE_INPUTS
    assert manifest["tested_commit"] == record["tested_commit"]
