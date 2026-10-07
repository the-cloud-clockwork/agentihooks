import ast
import copy
import fcntl
import json
import threading
from pathlib import Path

import pytest

import scripts.swarm_v2.contracts as contracts

FIXTURES = Path(__file__).parent / "fixtures"
SCHEMAS = Path(contracts.__file__).resolve().parents[2] / "docs" / "swarm-v2" / "schemas"
AUTHORITY_CONTRACTS = ["launch", "heartbeat", "command", "checkpoint", "session_event", "context_pack", "receipt"]


@pytest.fixture
def loaded():
    return contracts.load()


def _fixture(contract, family="current"):
    return json.loads((FIXTURES / contract / f"{family}.json").read_text())


def _snapshot(store: Path) -> dict:
    return {str(p.relative_to(store)): p.read_bytes() for p in sorted(store.rglob("*")) if p.is_file()}


def _files(store: Path) -> list[str]:
    return sorted(str(p.relative_to(store)) for p in store.rglob("*") if p.is_file())


def _refusal(reason, detail, operation_id, error="invalid_request", retry="new_request"):
    return {
        "schema_version": "2.1",
        "operation_id": operation_id,
        "error": error,
        "reason": reason,
        "retry": retry,
        "detail": detail,
    }


@pytest.mark.parametrize("contract", AUTHORITY_CONTRACTS + ["error"])
@pytest.mark.parametrize("family", ["current", "previous-minor", "newer-minor"])
def test_supported_families_pass_the_check(loaded, contract, family):
    assert contracts.check(loaded, contract, _fixture(contract, family)) is None
    assert contracts.contract_validation_failures_total(loaded) == {}


@pytest.mark.parametrize("contract", AUTHORITY_CONTRACTS)
def test_admitting_current_and_previous_minor_records_writes_them(loaded, tmp_path, contract):
    for family in ("previous-minor", "current"):
        doc = _fixture(contract, family)
        doc["operation_id"] = f"{family}-op"
        assert contracts.admit(loaded, tmp_path / "store", contract, doc) == {
            "state": "accepted",
            "operation_id": f"{family}-op",
        }
        stored = json.loads((tmp_path / "store" / contract / f"{family}-op.json").read_text())
        assert stored == doc
    generations = json.loads((tmp_path / "store" / "generations.json").read_text())
    assert generations == {"implement-session-ingestion": {"generation": 3, "execution_id": "exe-synthetic-0007"}}


def test_a_second_independent_store_accepts_without_state_from_the_first(loaded, tmp_path):
    doc = _fixture("heartbeat")
    assert contracts.admit(loaded, tmp_path / "one", "heartbeat", doc)["state"] == "accepted"
    assert contracts.admit(loaded, tmp_path / "two", "heartbeat", doc)["state"] == "accepted"
    assert _snapshot(tmp_path / "one") == _snapshot(tmp_path / "two")


@pytest.mark.parametrize("contract", AUTHORITY_CONTRACTS + ["error"])
def test_a_future_major_is_refused_before_any_write(loaded, tmp_path, contract):
    doc = _fixture(contract, "future-major")
    expected = _refusal(
        "unsupported_major", "schema major 3 is not supported; this reader accepts major 2", doc["operation_id"]
    )
    assert contracts.check(loaded, contract, doc) == expected
    if contract != "error":
        assert contracts.admit(loaded, tmp_path / "store", contract, doc) == expected
        assert _snapshot(tmp_path / "store") == {}
    assert contracts.contract_validation_failures_total(loaded) == {
        f"{contract}/unsupported_major": 2 if contract != "error" else 1
    }


@pytest.mark.parametrize("contract", AUTHORITY_CONTRACTS)
@pytest.mark.parametrize("family", ["missing-authority", "previous-minor-missing-authority"])
def test_an_absent_execution_generation_is_refused_before_any_write(loaded, tmp_path, contract, family):
    seeded = contracts.admit(loaded, tmp_path / "store", contract, _fixture(contract))
    assert seeded["state"] == "accepted"
    before = _snapshot(tmp_path / "store")
    doc = _fixture(contract, family)
    doc["operation_id"] = "another-op"
    expected = _refusal("missing_authority", "authority lacks task_generation", "another-op")
    assert contracts.admit(loaded, tmp_path / "store", contract, doc) == expected
    assert _snapshot(tmp_path / "store") == before
    assert contracts.contract_validation_failures_total(loaded) == {f"{contract}/missing_authority": 1}


def test_a_record_without_authority_names_every_missing_field(loaded):
    doc = _fixture("command")
    del doc["authority"]
    assert contracts.check(loaded, "command", doc)["detail"] == (
        "authority lacks execution_id, task_id, task_generation, controller_epoch"
    )


def test_the_counter_keeps_one_entry_per_contract_and_reason(loaded):
    contracts.check(loaded, "launch", _fixture("launch", "future-major"))
    contracts.check(loaded, "launch", _fixture("launch", "future-major"))
    contracts.check(loaded, "launch", _fixture("launch", "missing-authority"))
    contracts.check(loaded, "receipt", _fixture("receipt", "future-major"))
    assert contracts.contract_validation_failures_total(loaded) == {
        "launch/unsupported_major": 2,
        "launch/missing_authority": 1,
        "receipt/unsupported_major": 1,
    }


@pytest.mark.parametrize(
    ("version", "reason", "detail"),
    [
        (None, "missing_version", "schema_version must be MAJOR.MINOR"),
        ("2", "missing_version", "schema_version must be MAJOR.MINOR"),
        ("v2.1", "missing_version", "schema_version must be MAJOR.MINOR"),
        ("2.1.0", "missing_version", "schema_version must be MAJOR.MINOR"),
        (2.1, "missing_version", "schema_version must be MAJOR.MINOR"),
        ("2x1", "missing_version", "schema_version must be MAJOR.MINOR"),
        ("\u0662.1", "missing_version", "schema_version must be MAJOR.MINOR"),
        ("2.\u0661", "missing_version", "schema_version must be MAJOR.MINOR"),
        ("2a.1", "missing_version", "schema_version must be MAJOR.MINOR"),
        ("1.9", "unsupported_major", "schema major 1 is not supported; this reader accepts major 2"),
    ],
)
def test_an_unreadable_or_older_major_version_is_refused(loaded, version, reason, detail):
    doc = _fixture("heartbeat")
    doc["schema_version"] = version
    assert contracts.check(loaded, "heartbeat", doc) == _refusal(reason, detail, doc["operation_id"])


def test_a_minor_below_the_oldest_supported_is_refused(loaded):
    narrowed = copy.deepcopy(loaded)
    narrowed["compat"]["contracts"]["heartbeat"]["oldest_minor"] = 1
    doc = _fixture("heartbeat", "previous-minor")
    assert contracts.check(narrowed, "heartbeat", doc) == _refusal(
        "unsupported_minor", "schema minor 0 is older than the oldest supported minor 1", doc["operation_id"]
    )
    assert contracts.check(narrowed, "heartbeat", _fixture("heartbeat")) is None
    assert contracts.contract_validation_failures_total(narrowed) == {"heartbeat/unsupported_minor": 1}


def test_a_non_object_record_is_refused_with_an_unknown_operation(loaded):
    assert contracts.check(loaded, "launch", ["not", "a", "record"]) == _refusal(
        "not_an_object", "a record must be a JSON object", "unknown"
    )


@pytest.mark.parametrize("operation_id", [None, "../escape", "", 7, "a" * 129])
def test_a_malformed_operation_id_is_never_echoed(loaded, tmp_path, operation_id):
    doc = _fixture("launch")
    doc["operation_id"] = operation_id
    refusal = contracts.admit(loaded, tmp_path / "store", "launch", doc)
    assert refusal["operation_id"] == "unknown"
    assert refusal["reason"] == "schema_invalid"
    assert _snapshot(tmp_path / "store") == {}


def test_an_unknown_authority_field_is_refused_not_discarded(loaded):
    doc = _fixture("heartbeat")
    doc["authority"]["scope"] = "fleet"
    assert contracts.check(loaded, "heartbeat", doc) == _refusal(
        "schema_invalid", "authority: fails additionalProperties", doc["operation_id"]
    )


def test_an_authority_name_outside_the_authority_object_is_refused(loaded):
    doc = _fixture("command", "newer-minor")
    doc["task_generation"] = 9
    assert contracts.check(loaded, "command", doc) == _refusal(
        "schema_invalid", "<root>: fails not", doc["operation_id"]
    )


def test_a_schema_failure_names_the_path_and_keyword_but_not_the_value(loaded):
    doc = _fixture("launch")
    doc["image_digest"] = "sha256:private-value-must-not-echo"
    refusal = contracts.check(loaded, "launch", doc)
    assert refusal == _refusal("schema_invalid", "image_digest: fails pattern", doc["operation_id"])
    assert "private-value" not in json.dumps(refusal)


def test_a_missing_body_field_names_only_the_missing_names(loaded):
    doc = _fixture("receipt")
    del doc["receipt_id"]
    del doc["result_state"]
    assert contracts.check(loaded, "receipt", doc)["detail"] == "<root>: missing receipt_id, result_state"


def test_every_refusal_is_a_valid_error_envelope(loaded, tmp_path):
    validator = loaded["validators"]["error"]
    refusals = [
        contracts.check(loaded, "launch", _fixture("launch", "future-major")),
        contracts.check(loaded, "launch", _fixture("launch", "missing-authority")),
        contracts.check(loaded, "launch", ["x"]),
    ]
    stale = _fixture("checkpoint")
    contracts.admit(loaded, tmp_path / "store", "checkpoint", stale)
    older = _fixture("checkpoint")
    older["operation_id"] = "older-op"
    older["authority"]["task_generation"] = 2
    refusals.append(contracts.admit(loaded, tmp_path / "store", "checkpoint", older))
    for refusal in refusals:
        assert list(validator.iter_errors(refusal)) == []


def test_a_newer_minor_optional_field_survives_a_relay_round_trip(loaded, tmp_path):
    doc = _fixture("session_event", "newer-minor")
    assert contracts.admit(loaded, tmp_path / "store", "session_event", doc)["state"] == "accepted"
    relayed = json.loads((tmp_path / "store" / "session_event" / f"{doc['operation_id']}.json").read_text())
    assert relayed["handoff_hint"] == doc["handoff_hint"]
    relayed["payload"] = {"text": "Relayed with a changed known field."}
    relayed["operation_id"] = "relayed-op"
    assert contracts.check(loaded, "session_event", relayed) is None
    assert contracts.admit(loaded, tmp_path / "store", "session_event", relayed)["state"] == "accepted"
    again = json.loads((tmp_path / "store" / "session_event" / "relayed-op.json").read_text())
    assert again["handoff_hint"] == doc["handoff_hint"]
    assert again["schema_version"] == "2.2"


def test_a_replay_with_the_same_operation_has_no_second_effect(loaded, tmp_path):
    doc = _fixture("command")
    assert contracts.admit(loaded, tmp_path / "store", "command", doc)["state"] == "accepted"
    before = _snapshot(tmp_path / "store")
    assert contracts.admit(loaded, tmp_path / "store", "command", doc) == {
        "state": "replayed",
        "operation_id": doc["operation_id"],
    }
    assert _snapshot(tmp_path / "store") == before


def test_a_reused_operation_with_different_content_is_a_conflict(loaded, tmp_path):
    doc = _fixture("command")
    contracts.admit(loaded, tmp_path / "store", "command", doc)
    before = _snapshot(tmp_path / "store")
    changed = dict(doc, completion="succeeded")
    assert contracts.admit(loaded, tmp_path / "store", "command", changed) == _refusal(
        "operation_reused",
        "operation id already holds a different record",
        doc["operation_id"],
        error="revision_conflict",
    )
    assert _snapshot(tmp_path / "store") == before


def test_an_older_generation_cannot_overwrite_a_newer_accepted_record(loaded, tmp_path):
    newer = _fixture("checkpoint")
    newer["authority"]["task_generation"] = 4
    contracts.admit(loaded, tmp_path / "store", "checkpoint", newer)
    before = _snapshot(tmp_path / "store")
    older = _fixture("checkpoint")
    older["operation_id"] = "older-op"
    assert contracts.admit(loaded, tmp_path / "store", "checkpoint", older) == _refusal(
        "older_generation",
        "task generation is older than the accepted generation",
        "older-op",
        error="stale_generation",
        retry="never",
    )
    assert _snapshot(tmp_path / "store") == before
    assert contracts.contract_validation_failures_total(loaded) == {}


def test_a_display_label_grants_no_authority(loaded, tmp_path):
    newer = _fixture("launch")
    newer["authority"]["task_generation"] = 4
    contracts.admit(loaded, tmp_path / "store", "launch", newer)
    older = _fixture("launch")
    older["operation_id"] = "labelled-op"
    older["agent_name"] = "operator"
    older["runtime_profile"] = "task_generation=9"
    refusal = contracts.admit(loaded, tmp_path / "store", "launch", older)
    assert (refusal["error"], refusal["operation_id"]) == ("stale_generation", "labelled-op")


def test_the_same_generation_and_a_newer_one_are_both_accepted(loaded, tmp_path):
    first = _fixture("heartbeat")
    contracts.admit(loaded, tmp_path / "store", "heartbeat", first)
    same = dict(first, operation_id="same-generation")
    assert contracts.admit(loaded, tmp_path / "store", "heartbeat", same)["state"] == "accepted"
    newer = copy.deepcopy(first)
    newer["operation_id"] = "newer-generation"
    newer["authority"]["task_generation"] = 4
    assert contracts.admit(loaded, tmp_path / "store", "heartbeat", newer)["state"] == "accepted"
    other = copy.deepcopy(first)
    other["operation_id"] = "other-task"
    other["authority"]["task_id"] = "another-task"
    other["authority"]["task_generation"] = 1
    assert contracts.admit(loaded, tmp_path / "store", "heartbeat", other)["state"] == "accepted"
    generations = json.loads((tmp_path / "store" / "generations.json").read_text())
    assert generations == {
        "another-task": {"generation": 1, "execution_id": "exe-synthetic-0007"},
        "implement-session-ingestion": {"generation": 4, "execution_id": "exe-synthetic-0007"},
    }


def test_an_interrupted_record_write_recovers_on_retry_without_a_duplicate(loaded, tmp_path, monkeypatch):
    doc = _fixture("launch")
    real = Path.replace
    calls = []

    def fail_record(self, target):
        calls.append(Path(target).name)
        if Path(target).name == f"{doc['operation_id']}.json":
            raise OSError("transport cut")
        return real(self, target)

    monkeypatch.setattr(Path, "replace", fail_record)
    with pytest.raises(OSError, match="transport cut"):
        contracts.admit(loaded, tmp_path / "store", "launch", doc)
    assert calls == ["generations.json", f"{doc['operation_id']}.json"]
    assert _files(tmp_path / "store") == [".lock", "generations.json"]
    monkeypatch.setattr(Path, "replace", real)
    assert contracts.admit(loaded, tmp_path / "store", "launch", doc)["state"] == "accepted"
    assert contracts.admit(loaded, tmp_path / "store", "launch", doc)["state"] == "replayed"
    assert _files(tmp_path / "store") == [
        ".lock",
        "generations.json",
        f"launch/{doc['operation_id']}.json",
    ]


def test_admission_waits_for_the_store_lock_held_by_another_writer(loaded, tmp_path):
    folder = tmp_path / "store"
    folder.mkdir(parents=True)
    results = []
    with (folder / ".lock").open("w") as held:
        fcntl.flock(held, fcntl.LOCK_EX)
        writer = threading.Thread(
            target=lambda: results.append(
                contracts.admit(loaded, tmp_path / "store", "heartbeat", _fixture("heartbeat"))
            )
        )
        writer.start()
        writer.join(timeout=0.3)
        assert writer.is_alive()
        assert _files(folder) == [".lock"]
    writer.join(timeout=10)
    assert results == [{"state": "accepted", "operation_id": "heartbeat-synthetic-0007-0041"}]


def test_the_stored_record_is_canonical_json(loaded, tmp_path):
    doc = _fixture("heartbeat")
    contracts.admit(loaded, tmp_path / "store", "heartbeat", doc)
    text = (tmp_path / "store" / "heartbeat" / f"{doc['operation_id']}.json").read_text()
    assert text == json.dumps(doc, indent=2, sort_keys=True) + "\n"
    generations = (tmp_path / "store" / "generations.json").read_text()
    assert generations == (
        '{\n  "implement-session-ingestion": {\n    "execution_id": "exe-synthetic-0007",\n    "generation": 3\n  }\n}\n'
    )


def test_writers_use_the_write_minor_and_rollback_lowers_it(loaded):
    assert contracts.write_version(loaded, "launch") == "2.1"
    rolled = copy.deepcopy(loaded)
    rolled["compat"]["contracts"]["launch"]["write_minor"] = 0
    assert contracts.write_version(rolled, "launch") == "2.0"
    assert contracts.check(rolled, "launch", _fixture("launch")) is None
    assert contracts.check(rolled, "launch", _fixture("launch", "previous-minor")) is None


def test_load_reads_the_named_schema_folder(tmp_path):
    for path in SCHEMAS.glob("*.json"):
        (tmp_path / path.name).write_text(path.read_text())
    compat = json.loads((tmp_path / "compatibility.json").read_text())
    compat["contracts"]["launch"]["write_minor"] = 0
    (tmp_path / "compatibility.json").write_text(json.dumps(compat))
    assert contracts.write_version(contracts.load(tmp_path), "launch") == "2.0"
    assert contracts.write_version(contracts.load(), "launch") == "2.1"
    assert sorted(contracts.load()["validators"]) == sorted(AUTHORITY_CONTRACTS + ["error"])


def test_the_admission_module_imports_no_other_repository_package():
    tree = ast.parse(Path(contracts.__file__).read_text())
    imports = [alias.name for node in ast.walk(tree) if isinstance(node, ast.Import) for alias in node.names]
    imports += [node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)]
    assert sorted(imports) == [
        "collections",
        "fcntl",
        "json",
        "jsonschema",
        "jsonschema.exceptions",
        "pathlib",
        "re",
        "referencing",
    ]


def test_a_newer_generation_fences_every_contract_of_the_task(loaded, tmp_path):
    heartbeat = _fixture("heartbeat")
    heartbeat["authority"]["task_generation"] = 4
    assert contracts.admit(loaded, tmp_path / "store", "heartbeat", heartbeat)["state"] == "accepted"
    before = _snapshot(tmp_path / "store")
    refusal = contracts.admit(loaded, tmp_path / "store", "checkpoint", _fixture("checkpoint"))
    assert refusal == _refusal(
        "older_generation",
        "task generation is older than the accepted generation",
        "checkpoint-synthetic-0002",
        error="stale_generation",
        retry="never",
    )
    assert _snapshot(tmp_path / "store") == before


def test_another_execution_cannot_share_an_accepted_generation(loaded, tmp_path):
    contracts.admit(loaded, tmp_path / "store", "command", _fixture("command"))
    before = _snapshot(tmp_path / "store")
    rival = _fixture("command")
    rival["operation_id"] = "rival-op"
    rival["authority"]["execution_id"] = "exe-synthetic-0008"
    assert contracts.admit(loaded, tmp_path / "store", "command", rival) == _refusal(
        "generation_held",
        "another execution holds this task generation",
        "rival-op",
        error="stale_generation",
        retry="never",
    )
    assert _snapshot(tmp_path / "store") == before
    rival["authority"]["task_generation"] = 4
    assert contracts.admit(loaded, tmp_path / "store", "command", rival)["state"] == "accepted"
