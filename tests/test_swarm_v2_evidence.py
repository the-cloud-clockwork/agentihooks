import copy
import fcntl
import hashlib
import json
import shutil
import subprocess
import sys
import threading
from pathlib import Path

import pytest

import scripts.swarm_v2.validate_plan as vp

ROOT = Path(__file__).resolve().parents[1]
INDEX = ROOT / "docs" / "swarm-v2" / "evidence-index.json"
MARKDOWN = ROOT / "docs" / "swarm-v2" / "evidence-index.md"
FIXTURES = Path(__file__).parent / "fixtures" / "swarm_v2" / "evidence"
PLAN = vp.load_plan(ROOT / "Swarm-v2.md")
SCREENSHOT = "tests/fixtures/swarm_v2/evidence/healthy-pod.png"
PULL = "https://github.com/the-cloud-clockwork/agentihooks/pull/1500"
COMMIT = "a" * 40


def _index(packages=None):
    index = vp.load_index(INDEX)
    if packages is not None:
        index["packages"] = json.loads((FIXTURES / packages).read_text())
    return index


def _check(index):
    return vp.check(index, PLAN, ROOT)


def _findings(index):
    return [(f["package"], f["reason"]) for f in _check(index)["findings"]]


def _errors(index):
    return _check(index)["errors"]


def _registry(tmp_path, packages=None):
    path = tmp_path / "evidence-index.json"
    path.write_text(json.dumps(_index(packages), indent=2) + "\n")
    return path


def _change(operation="op-1", base_revision=1, package="SV2-FND-04", evidence=(), **fields):
    return {
        "operation": operation,
        "base_revision": base_revision,
        "package": package,
        "evidence": list(evidence),
        **fields,
    }


def _experiment(eid="SV2-FND-04/first-try", outcome="failed"):
    return {"id": eid, "kind": "experiment", "case": "C", "ref": f"{PULL}#run-1", "outcome": outcome}


def _pull(eid="SV2-FND-04/pull-request", ref=PULL, commit=COMMIT):
    return {"id": eid, "kind": "pull_request", "ref": ref, "commit": commit}


def _test(case, eid=None, outcome="passed", kind="test"):
    return {
        "id": eid or f"SV2-FND-04/case-{case.lower()}",
        "kind": kind,
        "case": case,
        "ref": f"https://github.com/the-cloud-clockwork/agentihooks/actions/runs/{ord(case)}",
        "commit": COMMIT,
        "outcome": outcome,
    }


def _complete(*evidence):
    return {"claim": "complete", "evidence": list(evidence)}


def _alone(package, entry):
    index = _index()
    index["packages"][package] = entry
    return index


def _run(*args):
    return subprocess.run(
        [sys.executable, "-m", "scripts.swarm_v2.validate_plan", *map(str, args)],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )


# T-SV2-FND-04-A


def test_a_plan_reader_returns_every_package_with_repository_gate_dependencies_and_cases():
    assert len(PLAN["packages"]) == 150
    assert PLAN["packages"]["SV2-FND-03"] == {
        "repository": "agentihooks",
        "gate": "G0",
        "dependencies": ["SV2-FND-02"],
        "cases": {"A": "T-SV2-FND-03-A", "B": "T-SV2-FND-03-B", "C": "T-SV2-FND-03-C"},
    }
    assert PLAN["packages"]["SV2-FND-01"]["dependencies"] == []
    assert PLAN["packages"]["SV2-CAP-01"]["repository"] == "antoncore"
    assert PLAN["packages"]["SV2-REL-05"]["gate"] == "G12"


def test_a_plan_reader_returns_every_invariant_in_order():
    assert len(PLAN["invariants"]) == 34
    assert PLAN["invariants"][:2] == ["INV-R01", "INV-R02"]
    assert PLAN["invariants"][-1] == "INV-B12"


def test_a_gate_map_lists_each_gate_with_its_packages_in_plan_order():
    gates = vp.gates(PLAN)
    assert gates["G0"] == [f"SV2-FND-0{n}" for n in range(1, 6)]
    assert list(gates)[:3] == ["G0", "G1", "G2"]
    assert sum(len(p) for p in gates.values()) == 150


def test_a_committed_registry_maps_every_invariant_and_checks_clean():
    result = _check(_index())
    assert result == {"errors": [], "findings": [], "measurements": {"packages_missing_evidence": 0}}
    assert [r["id"] for r in _index()["requirements"]] == PLAN["invariants"]


def test_a_fully_evidenced_package_has_no_finding_and_the_screenshot_package_lacks_recovery_proof():
    result = _check(_index("packages.json"))
    assert result["errors"] == []
    assert result["measurements"] == {"packages_missing_evidence": 1}
    assert [f["package"] for f in result["findings"]] == ["SV2-SES-04"] * 5
    assert (
        "SV2-SES-04",
        "T-SV2-SES-04-C: missing recovery proof; a screenshot cannot satisfy transcript durability acceptance",
    ) in _findings(_index("packages.json"))


def test_a_second_independent_fixture_flags_only_its_screenshot_package():
    result = _check(_index("packages-second.json"))
    assert result["errors"] == []
    assert result["measurements"] == {"packages_missing_evidence": 1}
    assert {f["package"] for f in result["findings"]} == {"SV2-MUL-03"}
    assert (
        "SV2-MUL-03",
        "T-SV2-MUL-03-C: missing recovery proof; a screenshot cannot satisfy scope isolation acceptance",
    ) in _findings(_index("packages-second.json"))


def test_a_committed_markdown_is_rendered_from_the_registry():
    assert MARKDOWN.read_text() == vp.render(_index(), PLAN, _check(_index()))


def test_a_render_lists_gates_requirements_claims_and_the_measurement():
    text = vp.render(_index("packages.json"), PLAN, _check(_index("packages.json")))
    assert "| G0 | SV2-FND-01, SV2-FND-02, SV2-FND-03, SV2-FND-04, SV2-FND-05 |" in text
    assert "| INV-M01 | transcript durability | SV2-SES-04, SV2-IDX-01, SV2-VAL-03 |" in text
    assert "| SV2-FND-01 | agentihooks | G0 | complete | 4 | none |" in text
    assert "| SV2-SES-04 | agentihooks | G4 | complete | 1 | 5 |" in text
    findings = text.split("## Findings\n\n")[1].split("\n\n")[0]
    durability = "; a screenshot cannot satisfy transcript durability acceptance"
    assert findings.splitlines() == [
        "- SV2-SES-04: no pull request with a tested commit",
        f"- SV2-SES-04: T-SV2-SES-04-A: missing positive case result{durability}",
        f"- SV2-SES-04: T-SV2-SES-04-B: missing negative case result{durability}",
        f"- SV2-SES-04: T-SV2-SES-04-C: missing recovery proof{durability}",
        "- SV2-SES-04: dependency SV2-SES-03 lacks complete evidence",
    ]
    assert text.endswith("## Failed experiments\n\nNone.\n\npackages_missing_evidence: 1\n")


def test_a_check_command_prints_clean_for_the_committed_registry():
    result = _run("check")
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {"errors": [], "findings": [], "measurements": {"packages_missing_evidence": 0}}


def test_a_record_appends_evidence_and_a_claim_at_the_next_revision(tmp_path):
    path = _registry(tmp_path)
    result = vp.record(path, _change(evidence=[_pull()], claim="complete"), PLAN, ROOT)
    assert result == {
        "operation": "op-1",
        "revision": 2,
        "package": "SV2-FND-04",
        "claim": "complete",
        "added": ["SV2-FND-04/pull-request"],
    }
    stored = vp.load_index(path)
    assert stored["revision"] == 2
    assert stored["packages"]["SV2-FND-04"] == {"claim": "complete", "evidence": [_pull()]}
    assert stored["operations"][-1]["id"] == "op-1"
    assert stored["operations"][-1]["revision"] == 2


def test_a_record_without_a_claim_keeps_a_new_package_open(tmp_path):
    path = _registry(tmp_path)
    result = vp.record(path, _change(evidence=[_test("A")]), PLAN, ROOT)
    assert result["claim"] == "open"
    assert vp.load_index(path)["packages"]["SV2-FND-04"]["claim"] == "open"


def test_a_fully_evidenced_recorded_package_checks_clean(tmp_path):
    path = _registry(tmp_path)
    evidence = [_pull(), _test("A"), _test("B"), _test("C", kind="live_canary")]
    vp.record(path, _change(evidence=evidence, claim="complete"), PLAN, ROOT)
    assert _findings(vp.load_index(path)) == []


def test_a_record_command_writes_the_registry_and_its_markdown(tmp_path):
    path = _registry(tmp_path)
    change = tmp_path / "change.json"
    change.write_text(json.dumps(_change(evidence=[_pull()])))
    markdown = tmp_path / "index.md"
    result = _run("record", "--index", path, "--change", change, "--markdown", markdown)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["revision"] == 2
    index = vp.load_index(path)
    assert markdown.read_text() == vp.render(index, PLAN, _check(index))


# T-SV2-FND-04-B


def test_a_screenshot_named_for_the_recovery_case_still_does_not_satisfy_it():
    shot = {"id": "SV2-FND-04/shot", "kind": "screenshot", "case": "C", "ref": f"{PULL}#shot"}
    index = _alone("SV2-FND-04", _complete(_pull(), _test("A"), _test("B"), shot))
    assert _findings(index) == [
        ("SV2-FND-04", "T-SV2-FND-04-C: missing recovery proof; a screenshot cannot satisfy acceptance")
    ]


def test_a_screenshot_for_another_case_is_not_named_on_the_recovery_finding():
    shot = {"id": "SV2-FND-04/shot", "kind": "screenshot", "case": "A", "ref": f"{PULL}#shot"}
    index = _alone("SV2-FND-04", _complete(_pull(), _test("A"), _test("B"), shot))
    assert _findings(index) == [("SV2-FND-04", "T-SV2-FND-04-C: missing recovery proof")]


def test_every_missing_case_and_the_pull_request_are_named():
    index = _alone("SV2-FND-04", _complete())
    assert _findings(index) == [
        ("SV2-FND-04", "no pull request with a tested commit"),
        ("SV2-FND-04", "T-SV2-FND-04-A: missing positive case result"),
        ("SV2-FND-04", "T-SV2-FND-04-B: missing negative case result"),
        ("SV2-FND-04", "T-SV2-FND-04-C: missing recovery proof"),
    ]


def test_a_failed_test_does_not_satisfy_its_case():
    index = _alone("SV2-FND-04", _complete(_pull(), _test("A", outcome="failed"), _test("B"), _test("C")))
    assert _findings(index) == [("SV2-FND-04", "T-SV2-FND-04-A: missing positive case result")]


def test_a_failed_experiment_does_not_satisfy_its_case():
    index = _alone("SV2-FND-04", _complete(_pull(), _test("A"), _test("B"), _experiment(outcome="passed")))
    assert _findings(index) == [("SV2-FND-04", "T-SV2-FND-04-C: missing recovery proof")]


def test_a_pull_request_from_another_repository_does_not_count():
    other = _pull(ref="https://github.com/the-cloud-clockwork/antoncore/pull/7")
    index = _alone("SV2-FND-04", _complete(other, _test("A"), _test("B"), _test("C")))
    assert _findings(index) == [("SV2-FND-04", "no pull request with a tested commit")]


def test_a_dependency_without_a_complete_claim_is_named():
    index = _alone("SV2-FND-04", _complete(_pull(), _test("A"), _test("B"), _test("C")))
    index["packages"]["SV2-FND-03"] = {"claim": "open", "evidence": []}
    assert _findings(index) == [("SV2-FND-04", "dependency SV2-FND-03 lacks complete evidence")]


def test_a_dependency_claimed_complete_without_its_evidence_is_named():
    index = _index()
    index["packages"]["SV2-FND-04"] = _complete(_pull(), _test("A"), _test("B"), _test("C"))
    index["packages"]["SV2-FND-03"]["evidence"].pop()
    assert _findings(index) == [
        ("SV2-FND-03", "T-SV2-FND-03-C: missing recovery proof"),
        ("SV2-FND-04", "dependency SV2-FND-03 lacks complete evidence"),
    ]


def test_an_open_package_is_never_a_finding():
    index = _alone("SV2-FND-04", {"claim": "open", "evidence": []})
    assert _check(index)["findings"] == []


@pytest.mark.parametrize(
    ("field", "value", "error"),
    [
        (
            "ref",
            "CI passed and the pod looks healthy",
            "ref must be an https link or a repository file with its sha256",
        ),
        ("ref", "http://example.com/run", "ref must be an https link or a repository file with its sha256"),
        ("ref", "https://example.com/a b", "ref must be an https link or a repository file with its sha256"),
        ("ref", "/etc/passwd", "ref must be an https link or a repository file with its sha256"),
        ("kind", "summary", "unknown kind 'summary'"),
        ("commit", "abc", "commit must be a full 40 character sha"),
        ("case", "D", "case must be one of A, B, C"),
        ("outcome", "green", "outcome must be passed or failed"),
    ],
)
def test_malformed_evidence_is_refused(field, value, error):
    index = _alone("SV2-FND-04", _complete({**_test("A"), field: value}))
    assert _errors(index) == [f"SV2-FND-04/case-a: {error}"]


def test_a_test_without_a_commit_or_outcome_is_refused():
    item = {k: v for k, v in _test("A").items() if k not in ("commit", "outcome")}
    index = _alone("SV2-FND-04", _complete(item))
    assert _errors(index) == [
        "SV2-FND-04/case-a: commit must be a full 40 character sha",
        "SV2-FND-04/case-a: outcome must be passed or failed",
    ]


def test_a_missing_id_is_refused():
    item = {k: v for k, v in _test("A").items() if k != "id"}
    assert _errors(_alone("SV2-FND-04", _complete(item))) == ["SV2-FND-04: evidence needs an id"]


def test_a_repository_file_must_exist_and_match_its_sha256():
    index = _index()
    case = index["packages"]["SV2-FND-01"]["evidence"][1]
    case["sha256"] = "0" * 64
    gone = {**_test("A", eid="SV2-FND-04/gone"), "ref": "evidence/SV2-FND-04/none.json", "sha256": "0" * 64}
    escape = {**_test("B", eid="SV2-FND-04/escape"), "ref": "../outside.json", "sha256": "0" * 64}
    index["packages"]["SV2-FND-04"] = {"claim": "open", "evidence": [gone, escape]}
    assert _errors(index) == [
        "SV2-FND-01/case-a: evidence/SV2-FND-01/a-result.json does not match its recorded sha256",
        "SV2-FND-04/gone: evidence/SV2-FND-04/none.json is missing",
        "SV2-FND-04/escape: ref must be an https link or a repository file with its sha256",
    ]


def test_a_repository_file_without_a_sha256_is_refused():
    item = {**_test("A"), "ref": "evidence/SV2-FND-01/a-result.json"}
    assert _errors(_alone("SV2-FND-04", _complete(item))) == [
        "SV2-FND-04/case-a: ref must be an https link or a repository file with its sha256"
    ]


def test_registry_structure_errors_are_named():
    index = _index()
    index["requirements"][0]["class"] = "vibes"
    index["requirements"][1]["packages"] = ["SV2-XYZ-01"]
    index["requirements"].pop()
    index["requirements"].append({"id": "INV-Z01", "class": "scope_isolation", "packages": ["SV2-FND-01"]})
    index["packages"]["SV2-NOPE-01"] = {"claim": "open", "evidence": []}
    index["packages"]["SV2-FND-04"] = {"claim": "done", "evidence": [_pull(), _pull()]}
    assert _errors(index) == [
        "INV-R01: unknown class 'vibes'",
        "INV-R02: names unknown package SV2-XYZ-01",
        "INV-Z01 is not an invariant of the plan",
        "INV-B12 is not mapped to any package",
        "SV2-NOPE-01 is not a package of the plan",
        "SV2-FND-04: unknown claim 'done'",
        "SV2-FND-04/pull-request is recorded more than once",
    ]


def test_a_requirement_with_no_packages_is_unmapped():
    index = _index()
    index["requirements"][0]["packages"] = []
    assert _errors(index) == ["INV-R01 is not mapped to any package"]


def test_load_index_refuses_another_schema(tmp_path):
    path = tmp_path / "other.json"
    path.write_text(json.dumps({"schema": "swarm-v2-architecture/1"}))
    with pytest.raises(vp.EvidenceError, match=f"^{path} is not a swarm-v2-evidence-index/1 document$"):
        vp.load_index(path)


def test_a_check_command_exits_one_on_findings(tmp_path):
    path = _registry(tmp_path, "packages.json")
    result = _run("check", "--index", path)
    assert result.returncode == 1
    assert json.loads(result.stdout)["measurements"] == {"packages_missing_evidence": 1}


def test_a_check_command_exits_one_on_errors(tmp_path):
    index = _alone("SV2-FND-04", _complete({**_test("A"), "ref": "it works"}))
    path = tmp_path / "evidence-index.json"
    path.write_text(json.dumps(index))
    result = _run("check", "--index", path)
    assert result.returncode == 1
    assert json.loads(result.stdout)["errors"] != []


def test_a_command_reports_a_refusal_on_stderr_with_exit_two(tmp_path):
    path = tmp_path / "other.json"
    path.write_text(json.dumps({"schema": "x"}))
    result = _run("check", "--index", path)
    assert result.returncode == 2
    assert result.stderr == f"error: {path} is not a swarm-v2-evidence-index/1 document\n"


def _refused(path, call, message):
    before = path.read_bytes()
    with pytest.raises(vp.EvidenceError) as caught:
        call()
    assert str(caught.value) == message
    assert path.read_bytes() == before


def test_a_record_refuses_a_stale_base_revision_without_writing(tmp_path):
    path = _registry(tmp_path)
    _refused(
        path,
        lambda: vp.record(path, _change(base_revision=0, evidence=[_pull()]), PLAN, ROOT),
        "change is based on revision 0; the registry is at revision 1",
    )


def test_a_record_refuses_an_unknown_package_without_writing(tmp_path):
    path = _registry(tmp_path)
    _refused(
        path,
        lambda: vp.record(path, _change(package="SV2-NOPE-01"), PLAN, ROOT),
        "SV2-NOPE-01 is not a package of the plan",
    )


def test_a_record_refuses_an_unknown_claim_without_writing(tmp_path):
    path = _registry(tmp_path)
    _refused(path, lambda: vp.record(path, _change(claim="done"), PLAN, ROOT), "SV2-FND-04: unknown claim 'done'")


def test_a_record_refuses_prose_evidence_without_writing(tmp_path):
    path = _registry(tmp_path)
    prose = {**_test("A"), "ref": "all tests pass"}
    _refused(
        path,
        lambda: vp.record(path, _change(evidence=[prose]), PLAN, ROOT),
        "SV2-FND-04/case-a: ref must be an https link or a repository file with its sha256",
    )


def test_recorded_evidence_is_immutable(tmp_path):
    path = _registry(tmp_path)
    vp.record(path, _change(evidence=[_pull()]), PLAN, ROOT)
    _refused(
        path,
        lambda: vp.record(path, _change("op-2", 2, evidence=[_pull(commit="b" * 40)]), PLAN, ROOT),
        "evidence SV2-FND-04/pull-request is already recorded with different content",
    )


def test_an_evidence_id_from_another_package_is_refused(tmp_path):
    path = _registry(tmp_path)
    taken = {**_pull(), "id": "SV2-FND-03/pull-request"}
    _refused(
        path,
        lambda: vp.record(path, _change(evidence=[taken]), PLAN, ROOT),
        "evidence SV2-FND-03/pull-request is already recorded with different content",
    )


# T-SV2-FND-04-C


def test_a_replayed_record_has_no_second_effect(tmp_path):
    path = _registry(tmp_path)
    change = _change(evidence=[_pull()], claim="complete")
    first = vp.record(path, change, PLAN, ROOT)
    stored = path.read_bytes()
    assert vp.record(path, copy.deepcopy(change), PLAN, ROOT) == first
    assert path.read_bytes() == stored


def test_a_reused_operation_with_different_content_is_a_conflict(tmp_path):
    path = _registry(tmp_path)
    vp.record(path, _change(evidence=[_pull()]), PLAN, ROOT)
    _refused(
        path,
        lambda: vp.record(path, _change(evidence=[_test("A")]), PLAN, ROOT),
        "operation op-1 was already recorded with different content",
    )


def test_resending_identical_evidence_adds_nothing(tmp_path):
    path = _registry(tmp_path)
    vp.record(path, _change(evidence=[_pull()]), PLAN, ROOT)
    result = vp.record(path, _change("op-2", 2, evidence=[_pull(), _test("A")]), PLAN, ROOT)
    assert result["added"] == ["SV2-FND-04/case-a"]
    assert vp.load_index(path)["packages"]["SV2-FND-04"]["evidence"] == [_pull(), _test("A")]


def test_a_failed_experiment_survives_later_records_and_is_rendered(tmp_path):
    path = _registry(tmp_path)
    vp.record(path, _change(evidence=[_experiment()]), PLAN, ROOT)
    vp.record(path, _change("op-2", 2, evidence=[_pull()], claim="complete"), PLAN, ROOT)
    index = vp.load_index(path)
    assert index["packages"]["SV2-FND-04"]["evidence"][0] == _experiment()
    text = vp.render(index, PLAN, _check(index))
    assert f"## Failed experiments\n\n- SV2-FND-04: SV2-FND-04/first-try, case C, {PULL}#run-1\n" in text


def test_restoring_a_backup_preserves_ids_and_pull_request_links(tmp_path):
    path = _registry(tmp_path)
    vp.record(path, _change(evidence=[_pull(), _experiment()]), PLAN, ROOT)
    backup = tmp_path / "backup.json"
    shutil.copy(path, backup)
    path.write_text("{ torn write")
    result = vp.restore(path, backup, "restore-1", PLAN, ROOT)
    assert result == {
        "operation": "restore-1",
        "revision": 3,
        "restored_from": 2,
        "replaced": "unreadable",
        "packages": 4,
        "evidence": 14,
    }
    restored, saved = vp.load_index(path), vp.load_index(backup)
    assert restored["packages"] == saved["packages"]
    assert restored["requirements"] == saved["requirements"]
    assert restored["operations"][:-1] == saved["operations"]


def test_restoring_over_a_missing_registry_works(tmp_path):
    backup = _registry(tmp_path)
    path = tmp_path / "gone.json"
    result = vp.restore(path, backup, "restore-1", PLAN, ROOT)
    assert (result["revision"], result["replaced"]) == (2, "missing")
    assert vp.load_index(path)["packages"] == vp.load_index(backup)["packages"]


def test_a_stale_backup_cannot_overwrite_a_newer_registry(tmp_path):
    path = _registry(tmp_path)
    backup = tmp_path / "backup.json"
    shutil.copy(path, backup)
    vp.record(path, _change(evidence=[_pull()]), PLAN, ROOT)
    _refused(
        path,
        lambda: vp.restore(path, backup, "restore-1", PLAN, ROOT),
        "backup at revision 1 is older than the registry at revision 2",
    )


def test_a_backup_differing_at_the_same_revision_is_refused(tmp_path):
    path = _registry(tmp_path)
    backup = tmp_path / "backup.json"
    index = vp.load_index(path)
    index["packages"]["SV2-FND-04"] = {"claim": "open", "evidence": []}
    backup.write_text(json.dumps(index))
    _refused(
        path,
        lambda: vp.restore(path, backup, "restore-1", PLAN, ROOT),
        "backup at revision 1 differs from the registry at the same revision",
    )


def test_a_newer_backup_restores_over_an_older_registry(tmp_path):
    path = _registry(tmp_path)
    older = path.read_bytes()
    vp.record(path, _change(evidence=[_pull()]), PLAN, ROOT)
    backup = tmp_path / "backup.json"
    shutil.copy(path, backup)
    path.write_bytes(older)
    result = vp.restore(path, backup, "restore-1", PLAN, ROOT)
    assert (result["restored_from"], result["revision"], result["replaced"]) == (2, 3, "revision 1")
    assert vp.load_index(path)["packages"]["SV2-FND-04"]["evidence"] == [_pull()]


def test_an_identical_backup_at_the_same_revision_restores(tmp_path):
    path = _registry(tmp_path)
    backup = tmp_path / "backup.json"
    shutil.copy(path, backup)
    assert vp.restore(path, backup, "restore-1", PLAN, ROOT)["revision"] == 2


def test_an_invalid_backup_is_refused(tmp_path):
    path = _registry(tmp_path)
    backup = tmp_path / "backup.json"
    index = vp.load_index(path)
    index["packages"]["SV2-NOPE-01"] = {"claim": "open", "evidence": []}
    backup.write_text(json.dumps(index))
    path.unlink()
    with pytest.raises(vp.EvidenceError, match="^SV2-NOPE-01 is not a package of the plan$"):
        vp.restore(path, backup, "restore-1", PLAN, ROOT)
    assert not path.exists()


def test_a_replayed_restore_has_no_second_effect(tmp_path):
    path = _registry(tmp_path)
    backup = tmp_path / "backup.json"
    shutil.copy(path, backup)
    first = vp.restore(path, backup, "restore-1", PLAN, ROOT)
    stored = path.read_bytes()
    assert vp.restore(path, backup, "restore-1", PLAN, ROOT) == first
    assert path.read_bytes() == stored


def test_a_restore_command_rewrites_the_markdown(tmp_path):
    backup = _registry(tmp_path)
    path, markdown = tmp_path / "gone.json", tmp_path / "index.md"
    result = _run("restore", "--index", path, "--backup", backup, "--operation", "r1", "--markdown", markdown)
    assert result.returncode == 0, result.stderr
    index = vp.load_index(path)
    assert markdown.read_text() == vp.render(index, PLAN, _check(index))


def test_reopening_reverts_the_claim_without_touching_evidence(tmp_path):
    path = _registry(tmp_path)
    before = vp.load_index(path)["packages"]["SV2-FND-03"]["evidence"]
    result = vp.reopen(path, "SV2-FND-03", "reopen-1")
    assert result == {"operation": "reopen-1", "revision": 2, "package": "SV2-FND-03", "claim": "open"}
    after = vp.load_index(path)["packages"]["SV2-FND-03"]
    assert after == {"claim": "open", "evidence": before}
    assert _findings(vp.load_index(path)) == []


def test_a_replayed_reopen_has_no_second_effect(tmp_path):
    path = _registry(tmp_path)
    first = vp.reopen(path, "SV2-FND-03", "reopen-1")
    stored = path.read_bytes()
    assert vp.reopen(path, "SV2-FND-03", "reopen-1") == first
    assert path.read_bytes() == stored


def test_reopening_a_package_not_claimed_complete_is_refused(tmp_path):
    path = _registry(tmp_path)
    _refused(path, lambda: vp.reopen(path, "SV2-FND-04", "reopen-1"), "SV2-FND-04 is not claimed complete")


def test_a_reopen_command_reports_the_projection(tmp_path):
    path, markdown = _registry(tmp_path), tmp_path / "index.md"
    result = _run("reopen", "--index", path, "--package", "SV2-FND-03", "--operation", "o1", "--markdown", markdown)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["claim"] == "open"
    assert "| SV2-FND-03 | agentihooks | G0 | open | 4 | none |" in markdown.read_text()


def test_writes_leave_no_temporary_file(tmp_path):
    path = _registry(tmp_path)
    vp.record(path, _change(evidence=[_pull()]), PLAN, ROOT)
    assert [p.name for p in tmp_path.glob("evidence-index*")] == ["evidence-index.json"]


# Review round one


def _plan(packages, invariants=()):
    return {"packages": packages, "invariants": list(invariants)}


def _spec(*dependencies):
    cases = {c: f"T-SV2-ZZZ-01-{c}" for c in "ABC"}
    return {"repository": "agentihooks", "gate": "G0", "dependencies": list(dependencies), "cases": cases}


def test_a_plan_package_without_its_gate_or_cases_is_refused(tmp_path):
    head = "#### SV2-ZZZ-01: Sample\n\n- Repository: `agentihooks`.\n"
    case = "- Recovery case: [T-SV2-ZZZ-01-C](#t).\n"
    plan = tmp_path / "plan.md"
    plan.write_text(head + "- Integration gate: none.\n" + case)
    message = "^SV2-ZZZ-01 in the plan lacks its integration gate or one of its three acceptance cases$"
    with pytest.raises(vp.EvidenceError, match=message):
        vp.load_plan(plan)
    plan.write_text(head + "- Integration gate: `G0`, subject to gates.\n" + case)
    with pytest.raises(vp.EvidenceError, match=message):
        vp.load_plan(plan)


def test_a_dependency_cycle_is_named_instead_of_recursing():
    plan = _plan({"SV2-AAA-01": _spec("SV2-BBB-01"), "SV2-BBB-01": _spec("SV2-AAA-01")})
    full = [_pull(), _test("A"), _test("B"), _test("C")]
    index = {"requirements": [], "packages": {p: _complete(*full) for p in plan["packages"]}}
    index["packages"]["SV2-BBB-01"]["evidence"] = [{**item, "id": f"b-{i}"} for i, item in enumerate(full)]
    result = vp.check(index, plan, ROOT)
    assert result["findings"] == [
        {"package": "SV2-AAA-01", "reason": "dependency SV2-BBB-01 lacks complete evidence"},
        {"package": "SV2-BBB-01", "reason": "dependency SV2-AAA-01 lacks complete evidence"},
    ]


def test_case_results_from_another_commit_than_the_pull_request_do_not_count():
    index = _alone("SV2-FND-04", _complete(_pull(commit="b" * 40), _test("A"), _test("B"), _test("C")))
    assert _findings(index) == [
        ("SV2-FND-04", "T-SV2-FND-04-A: missing positive case result"),
        ("SV2-FND-04", "T-SV2-FND-04-B: missing negative case result"),
        ("SV2-FND-04", "T-SV2-FND-04-C: missing recovery proof"),
    ]


def test_a_second_pull_request_at_the_tested_commit_satisfies_the_cases():
    later = _pull(eid="SV2-FND-04/pull-request-2", commit="b" * 40)
    index = _alone("SV2-FND-04", _complete(later, _pull(), _test("A"), _test("B"), _test("C")))
    assert _findings(index) == []


@pytest.mark.parametrize("kind", ["test", "live_canary"])
def test_an_image_cannot_be_a_test_or_live_canary_result(kind):
    sha = hashlib.sha256((ROOT / SCREENSHOT).read_bytes()).hexdigest()
    shot = {**_test("C", kind=kind), "ref": SCREENSHOT, "sha256": sha}
    index = _alone("SV2-FND-04", _complete(_pull(), _test("A"), _test("B"), shot))
    assert _errors(index) == [
        "SV2-FND-04/case-c: a test or live canary result must be a GitHub Actions run or a text file in the repository"
    ]
    linked = {**_test("C", kind=kind), "ref": f"{PULL}/pod.JPG?size=1#top"}
    assert _errors(_alone("SV2-FND-04", _complete(linked))) == [
        "SV2-FND-04/case-c: a test or live canary result must be a GitHub Actions run or a text file in the repository"
    ]


def test_an_image_screenshot_or_experiment_is_not_refused():
    sha = hashlib.sha256((ROOT / SCREENSHOT).read_bytes()).hexdigest()
    shot = {"id": "SV2-FND-04/shot", "kind": "screenshot", "ref": SCREENSHOT, "sha256": sha}
    tried = {**_experiment(), "ref": f"{PULL}/pod.png"}
    assert _errors(_alone("SV2-FND-04", _complete(shot, tried))) == []


def test_non_string_evidence_fields_are_refused_without_crashing():
    items = [
        {**_test("A"), "kind": ["test"]},
        {**_test("B"), "case": ["B"]},
        {**_test("C"), "commit": [COMMIT], "ref": [PULL]},
        {**_pull(), "id": ["x"]},
        "not an object",
    ]
    assert _errors(_alone("SV2-FND-04", _complete(*items))) == [
        "SV2-FND-04/case-a: unknown kind ['test']",
        "SV2-FND-04/case-b: case must be one of A, B, C",
        "SV2-FND-04/case-c: ref must be an https link or a repository file with its sha256",
        "SV2-FND-04/case-c: commit must be a full 40 character sha",
        "SV2-FND-04: evidence needs an id",
        "SV2-FND-04: evidence needs an id",
    ]


@pytest.mark.parametrize("item", [{**_pull(), "id": ["x"]}, "not an object", {"kind": "test"}])
def test_a_record_refuses_evidence_without_a_string_id_without_writing(tmp_path, item):
    path = _registry(tmp_path)
    _refused(
        path,
        lambda: vp.record(path, _change(evidence=[_pull(), item]), PLAN, ROOT),
        "SV2-FND-04: evidence needs an id",
    )


def test_a_backup_missing_recorded_operations_is_refused(tmp_path):
    path = _registry(tmp_path)
    fork = tmp_path / "fork.json"
    shutil.copy(path, fork)
    vp.record(path, _change("op-a", evidence=[_pull()]), PLAN, ROOT)
    vp.record(fork, _change("op-b", evidence=[_test("A")]), PLAN, ROOT)
    vp.record(fork, _change("op-c", 2, evidence=[_test("B")]), PLAN, ROOT)
    _refused(
        path,
        lambda: vp.restore(path, fork, "restore-1", PLAN, ROOT),
        "backup at revision 3 lacks operations the registry has recorded",
    )


def _waits_for_the_lock(tmp_path, call, args):
    results = []
    with (tmp_path / ".evidence-index.json.lock").open("w") as held:
        fcntl.flock(held, fcntl.LOCK_EX)
        writer = threading.Thread(target=lambda: results.append(call(*args)))
        writer.start()
        writer.join(timeout=0.3)
        assert writer.is_alive()
        assert vp.load_index(tmp_path / "evidence-index.json")["revision"] == 1
    writer.join(timeout=10)
    assert [r["revision"] for r in results] == [2]


def test_a_record_waits_for_the_registry_lock_held_by_another_writer(tmp_path):
    path = _registry(tmp_path)
    _waits_for_the_lock(tmp_path, vp.record, (path, _change(), PLAN, ROOT))


def test_a_restore_waits_for_the_registry_lock_held_by_another_writer(tmp_path):
    path = _registry(tmp_path)
    _waits_for_the_lock(tmp_path, vp.restore, (path, path, "op-1", PLAN, ROOT))


def test_a_reopen_waits_for_the_registry_lock_held_by_another_writer(tmp_path):
    path = _registry(tmp_path)
    _waits_for_the_lock(tmp_path, vp.reopen, (path, "SV2-FND-03", "op-1"))


def test_a_missing_registry_file_is_a_refusal_not_a_traceback(tmp_path):
    result = _run("check", "--index", tmp_path / "none.json")
    assert result.returncode == 2
    assert result.stderr == f"error: [Errno 2] No such file or directory: '{tmp_path / 'none.json'}'\n"


def test_a_change_without_an_operation_is_a_refusal_not_a_traceback(tmp_path):
    path = _registry(tmp_path)
    change = tmp_path / "change.json"
    change.write_text(json.dumps({"base_revision": 1, "package": "SV2-FND-04", "evidence": []}))
    result = _run("record", "--index", path, "--change", change, "--markdown", tmp_path / "index.md")
    assert (result.returncode, result.stderr) == (2, "error: change lacks operation\n")


def test_a_change_lacking_several_keys_names_each_without_writing(tmp_path):
    path = _registry(tmp_path)
    _refused(
        path,
        lambda: vp.record(path, {"package": "SV2-FND-04"}, PLAN, ROOT),
        "change lacks operation, base_revision, evidence",
    )


def test_malformed_registry_json_is_a_refusal_not_a_traceback(tmp_path):
    path = tmp_path / "evidence-index.json"
    path.write_text("{ torn")
    result = _run("check", "--index", path)
    assert (result.returncode, result.stderr) == (
        2,
        "error: Expecting property name enclosed in double quotes: line 1 column 3 (char 2)\n",
    )


@pytest.mark.parametrize("kind", ["test", "live_canary"])
def test_a_proof_must_be_an_actions_run_or_a_text_result_file(tmp_path, kind):
    disguised = tmp_path / "result.json"
    disguised.write_bytes((ROOT / SCREENSHOT).read_bytes())
    text = tmp_path / "result.log"
    text.write_text("3 passed\n")
    items = [
        {**_test("A", kind=kind), "ref": "https://example.com/pod?format=png"},
        {**_test("B", kind=kind), "ref": "https://github.com/the-cloud-clockwork/agentihooks/pull/1"},
        {**_test("C", kind=kind), "ref": "result.json", "sha256": hashlib.sha256(disguised.read_bytes()).hexdigest()},
        {
            **_test("C", eid="SV2-FND-04/log", kind=kind),
            "ref": "result.log",
            "sha256": hashlib.sha256(b"3 passed\n").hexdigest(),
        },
        {**_test("C", eid="SV2-FND-04/job", kind=kind), "ref": f"{PULL.split('/pull')[0]}/actions/runs/9/job/8"},
    ]
    index = {**_index(), "packages": {"SV2-FND-04": _complete(*items)}}
    message = "a test or live canary result must be a GitHub Actions run or a text file in the repository"
    assert vp.check(index, PLAN, tmp_path)["errors"] == [
        f"SV2-FND-04/case-a: {message}",
        f"SV2-FND-04/case-b: {message}",
        f"SV2-FND-04/case-c: {message}",
    ]
