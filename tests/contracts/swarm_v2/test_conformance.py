import json
import os
import shutil
import subprocess
import sys
from collections import Counter
from pathlib import Path

import pytest

import scripts.swarm_v2.contracts as contracts
from tests.contracts.swarm_v2 import conformance
from tests.contracts.swarm_v2.agentihooks_target import Target

ROOT = Path(__file__).resolve().parents[3]
SCHEMAS = ROOT / "docs" / "swarm-v2" / "schemas"
HERE = Path(__file__).parent
FIXTURES = HERE / "fixtures"
MATRIX_PATH = FIXTURES / "matrix.json"
COMPAT = json.loads((SCHEMAS / "compatibility.json").read_text())
MATRIX = json.loads(MATRIX_PATH.read_text())
SCOPED = {"launch", "checkpoint", "session_event", "context_pack", "receipt"}


class IgnoresGeneration(Target):
    def submit(self, contract, doc, grant):
        (self.root / "generations.json").unlink(missing_ok=True)
        return super().submit(contract, doc, grant)


class IgnoresScope(Target):
    def submit(self, contract, doc, grant):
        return super().submit(contract, doc, None)


class StripsExtensions(Target):
    def submit(self, contract, doc, grant):
        schema = json.loads((SCHEMAS / COMPAT["contracts"][contract]["schema"]).read_text())
        known = set(schema.get("properties", {})) | {"schema_version", "operation_id", "trace_id"}
        return super().submit(contract, {k: v for k, v in doc.items() if k in known}, grant)


class SucceedsOverHttp(Target):
    def submit(self, contract, doc, grant):
        return 200, super().submit(contract, doc, grant)[1]


class EchoesGrant(Target):
    def submit(self, contract, doc, grant):
        status, body = super().submit(contract, doc, grant)
        return status, {**body, "detail": f"{body.get('detail', '')} {grant}"} if "error" in body else body


class WritesBeforeRefusing(Target):
    def submit(self, contract, doc, grant):
        status, body = super().submit(contract, doc, grant)
        if "error" in body:
            (self.root / "refused.json").write_text(json.dumps(doc))
        return status, body


def _failed(report: dict) -> dict[str, list[str]]:
    return {r["case"]: r["failures"] for r in report["results"] if not r["passed"]}


def test_a_the_agentihooks_target_passes_every_conformance_case(tmp_path):
    report = conformance.run(MATRIX, SCHEMAS, Target, tmp_path)
    assert _failed(report) == {}
    assert report["contract_cases_passed"] == len(MATRIX["cases"])
    assert report["contract_cases_failed"] == 0
    published = conformance.publish(report, "agentihooks", "0" * 40, SCHEMAS, MATRIX_PATH)
    assert published["schemas_sha256"] == conformance.digest(SCHEMAS)
    report_dir = os.environ.get("PROOF_REPORT_DIR")
    if report_dir:
        Path(report_dir).mkdir(parents=True, exist_ok=True)
        Path(report_dir, "sv2-val-01-conformance.json").write_text(json.dumps(published, indent=2) + "\n")


def test_a_second_independent_run_reaches_the_same_verdicts(tmp_path):
    first = conformance.run(MATRIX, SCHEMAS, Target, tmp_path / "one")
    second = conformance.run(MATRIX, SCHEMAS, Target, tmp_path / "two")
    assert first == second


def test_a_matrix_covers_every_family_for_every_shared_contract():
    families = {}
    for case in MATRIX["cases"]:
        families.setdefault(case["contract"], set()).add(case["case"].split("/", 1)[1])
    assert set(families) == set(COMPAT["contracts"])
    base = {"current", "previous-minor", "newer-minor", "future-major", "extension", "invalid"}
    admitted = base | {
        "missing-authority",
        "previous-minor-missing-authority",
        "unknown-authority",
        "replay",
        "reused-operation",
        "older-generation",
        "held-generation",
        "newer-generation",
    }
    for contract, spec in COMPAT["contracts"].items():
        if not spec["authority"]:
            assert families[contract] == base
        elif contract in SCOPED:
            assert families[contract] == admitted | {"outside-grant"}
        else:
            assert families[contract] == admitted


def test_a_committed_matrix_matches_its_generator():
    assert MATRIX == conformance.build_matrix(FIXTURES, COMPAT)


def test_a_published_result_names_the_repository_commit_and_inputs(tmp_path):
    report = {"contract_cases_passed": 1, "contract_cases_failed": 0, "results": []}
    published = conformance.publish(report, "agentibrain-kernel", "a" * 40, SCHEMAS, MATRIX_PATH)
    assert published["repository"] == "agentibrain-kernel"
    assert published["commit"] == "a" * 40
    assert published["matrix_sha256"] == conformance.digest(MATRIX_PATH)
    assert published["contract_cases_passed"] == 1
    renamed = tmp_path / "schemas"
    shutil.copytree(SCHEMAS, renamed)
    (renamed / "error.json").write_text("{}")
    assert conformance.digest(renamed) != conformance.digest(SCHEMAS)


def test_a_vendored_runner_judges_a_target_without_the_producer_runtime(tmp_path):
    vendor = tmp_path / "vendor"
    vendor.mkdir()
    shutil.copy(HERE / "conformance.py", vendor / "conformance.py")
    (vendor / "echo_target.py").write_text(
        "import json\n"
        "class Target:\n"
        "    def __init__(self, root):\n"
        "        self.root = root\n"
        "    def submit(self, contract, doc, grant):\n"
        "        return 200, {'state': 'accepted', 'operation_id': doc['operation_id']}\n"
        "    def snapshot(self):\n"
        "        return {}\n"
        "    def stored(self, contract, operation_id):\n"
        "        return None\n"
    )
    proc = subprocess.run(
        [
            sys.executable,
            "-I",
            "-c",
            "import sys; sys.path.insert(0, '.'); import conformance; sys.exit(conformance.main(sys.argv[1:]))",
            str(SCHEMAS),
            str(MATRIX_PATH),
            "echo_target:Target",
            "synthetic-consumer",
            "b" * 40,
            str(tmp_path / "work"),
        ],
        cwd=vendor,
        capture_output=True,
        text=True,
        timeout=120,
    )
    report = json.loads(proc.stdout)
    assert proc.returncode == 1
    assert report["repository"] == "synthetic-consumer"
    assert report["contract_cases_failed"] > 0


def test_b_a_target_that_ignores_task_generation_fails_conformance(tmp_path):
    failed = _failed(conformance.run(MATRIX, SCHEMAS, IgnoresGeneration, tmp_path))
    assert {case.split("/", 1)[1] for case in failed} == {"older-generation", "held-generation"}
    assert "step 2: status 200 for a refused record" in failed["launch/older-generation"]
    assert "step 2: error None, expected 'stale_generation'" in failed["launch/older-generation"]


def test_b_a_target_that_ignores_project_scope_fails_conformance(tmp_path):
    failed = _failed(conformance.run(MATRIX, SCHEMAS, IgnoresScope, tmp_path))
    assert set(failed) == {f"{contract}/outside-grant" for contract in SCOPED}
    assert "step 1: a refused record changed stored state" in failed["receipt/outside-grant"]


def test_b_an_http_success_does_not_excuse_a_refusal(tmp_path):
    failed = _failed(conformance.run(MATRIX, SCHEMAS, SucceedsOverHttp, tmp_path))
    refusals = {c["case"] for c in MATRIX["cases"] if any("error" in s["expect"] for s in c.get("steps", []))}
    assert set(failed) == refusals
    assert failed["heartbeat/future-major"] == ["step 1: status 200 for a refused record"]


def test_b_a_target_that_discloses_the_caller_bearer_fails(tmp_path):
    failed = _failed(conformance.run(MATRIX, SCHEMAS, EchoesGrant, tmp_path))
    assert "step 1: the response discloses the caller bearer" in failed["launch/invalid"]


def test_b_a_refusal_that_mutates_stored_state_fails(tmp_path):
    failed = _failed(conformance.run(MATRIX, SCHEMAS, WritesBeforeRefusing, tmp_path))
    assert failed["command/unknown-authority"] == ["step 1: a refused record changed stored state"]


@pytest.mark.parametrize("contract", sorted(SCOPED))
def test_b_a_record_outside_the_grant_is_refused_before_any_write(tmp_path, contract):
    loaded = contracts.load()
    doc = json.loads((FIXTURES / contract / "current.json").read_text())
    key = next(k for k in conformance.SCOPE_KEYS if k in doc["authority"])
    grant = {k: doc["authority"][k] for k in conformance.SCOPE_KEYS if k in doc["authority"]}
    grant[key] = conformance.FOREIGN_SCOPE[key]
    refusal = contracts.admit(loaded, tmp_path, contract, doc, grant=grant)
    assert refusal == {
        "schema_version": contracts.write_version(loaded, "error"),
        "operation_id": doc["operation_id"],
        "error": "forbidden_scope",
        "reason": "outside_grant",
        "retry": "new_request",
        "detail": f"{key} is outside the caller grant",
    }
    assert conformance.FOREIGN_SCOPE[key] not in json.dumps(refusal)
    assert not list(tmp_path.rglob("*.json"))
    assert contracts.contract_validation_failures_total(loaded) == {f"{contract}/outside_grant": 1}


def test_b_a_launch_for_another_swarm_is_refused(tmp_path):
    loaded = contracts.load()
    doc = json.loads((FIXTURES / "launch" / "current.json").read_text())
    refusal = contracts.admit(
        loaded, tmp_path, "launch", doc, grant={"swarm_id": conformance.FOREIGN_SCOPE["swarm_id"]}
    )
    assert refusal["detail"] == "swarm_id is outside the caller grant"
    assert not list(tmp_path.rglob("*.json"))


def test_b_a_grant_without_shared_scope_keys_admits_the_record(tmp_path):
    loaded = contracts.load()
    doc = json.loads((FIXTURES / "heartbeat" / "current.json").read_text())
    assert contracts.admit(loaded, tmp_path, "heartbeat", doc, grant={"project_id": "local:elsewhere"}) == {
        "state": "accepted",
        "operation_id": doc["operation_id"],
    }


def test_b_a_corrected_record_needs_a_new_request_under_the_right_grant(tmp_path):
    loaded = contracts.load()
    doc = json.loads((FIXTURES / "launch" / "current.json").read_text())
    outside = {"project_id": conformance.FOREIGN_SCOPE["project_id"]}
    assert contracts.admit(loaded, tmp_path, "launch", doc, grant=outside)["retry"] == "new_request"
    assert contracts.admit(loaded, tmp_path, "launch", doc, grant=outside)["error"] == "forbidden_scope"
    assert contracts.contract_validation_failures_total(loaded) == {"launch/outside_grant": 2}
    inside = {"project_id": doc["authority"]["project_id"]}
    assert contracts.admit(loaded, tmp_path, "launch", doc, grant=inside)["state"] == "accepted"


def test_c_a_target_that_strips_extensions_fails_the_older_reader_case(tmp_path):
    failed = _failed(conformance.run(MATRIX, SCHEMAS, StripsExtensions, tmp_path))
    assert {case.split("/", 1)[1] for case in failed} == {"extension", "newer-minor"}
    assert failed["checkpoint/extension"] == ["step 1: the stored record differs from the accepted record"]


def test_c_an_extension_keeps_its_provenance_in_the_stored_record(tmp_path):
    case = next(c for c in MATRIX["cases"] if c["case"] == "session_event/extension")
    doc = case["steps"][0]["doc"]
    assert doc["relay_provenance"]["source"] == "synthetic-newer-producer"
    target = Target(tmp_path)
    assert target.submit("session_event", doc, case["grant"]) == (
        200,
        {"state": "accepted", "operation_id": doc["operation_id"]},
    )
    stored = target.stored("session_event", doc["operation_id"])
    assert stored["relay_provenance"] == doc["relay_provenance"]
    assert stored["extensions"] == doc["extensions"]


def test_c_unknown_optional_fields_pass_while_missing_authority_fails(tmp_path):
    verdicts = {r["case"]: r for r in conformance.run(MATRIX, SCHEMAS, Target, tmp_path)["results"]}
    assert verdicts["error/extension"]["passed"]
    assert verdicts["error/invalid"]["passed"]
    expects = Counter(
        (c["case"].split("/", 1)[1], c["steps"][0]["expect"].get("error", "accepted"))
        for c in MATRIX["cases"]
        if "steps" in c and len(c["steps"]) == 1
    )
    assert expects[("extension", "accepted")] == 7
    assert expects[("missing-authority", "invalid_request")] == 7


def test_c_an_older_generation_cannot_overwrite_a_newer_accepted_record(tmp_path):
    case = next(c for c in MATRIX["cases"] if c["case"] == "checkpoint/older-generation")
    target = Target(tmp_path)
    newer, older = (step["doc"] for step in case["steps"])
    assert target.submit("checkpoint", newer, case["grant"])[0] == 200
    held = target.snapshot()
    assert target.submit("checkpoint", older, case["grant"]) == (
        409,
        {
            "schema_version": "2.1",
            "operation_id": older["operation_id"],
            "error": "stale_generation",
            "reason": "older_generation",
            "retry": "never",
            "detail": "task generation is older than the accepted generation",
        },
    )
    assert target.snapshot() == held


def test_c_a_replay_with_the_same_operation_creates_no_duplicate(tmp_path):
    case = next(c for c in MATRIX["cases"] if c["case"] == "receipt/replay")
    target = Target(tmp_path)
    doc = case["steps"][0]["doc"]
    target.submit("receipt", doc, case["grant"])
    held = target.snapshot()
    assert target.submit("receipt", doc, case["grant"]) == (
        200,
        {"state": "replayed", "operation_id": doc["operation_id"]},
    )
    assert target.snapshot() == held
    assert len(list((tmp_path / "receipt").glob("*.json"))) == 1
