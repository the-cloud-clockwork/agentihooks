import hashlib
import json
from pathlib import Path

import pytest

from hooks.memory import cutover
from hooks.memory.api import records
from hooks.memory.cutover import ConsumerRoutes, shadow
from tests import sv2_mig04_cases as cases
from tests.sv2_idx04_cases import grant
from tests.sv2_idx05_cases import Bank

pytestmark = pytest.mark.unit
FIXTURE = Path(__file__).parent / "fixtures/swarm_v2/session-tools.json"
EVIDENCE = Path(__file__).parents[1] / "evidence" / "SV2-MIG-04"


@pytest.fixture
def bank(tmp_path):
    return Bank(json.loads(FIXTURE.read_text()), tmp_path)


def questions(bank):
    return [
        {
            "id": "catalog-lock",
            "text": '"database is locked"',
            "expected": [
                {
                    "session_uid": bank.uid("lock"),
                    "source_generation": bank.inputs["sessions"][0]["source_generation"],
                    "first_sequence": 1,
                    "last_sequence": 4,
                }
            ],
        }
    ]


def replay(bank):
    return shadow(bank.tools, grant(bank.inputs), questions(bank), 5000.0)


def test_shadow_resolves_source_ranges_in_two_independent_catalogs(bank, tmp_path):
    second = cases.bank(tmp_path / "independent", "independent")
    assert {item["session_uid"] for item in second.inputs["sessions"]}.isdisjoint(
        item["session_uid"] for item in bank.inputs["sessions"]
    )
    for instance in (bank, second):
        answer = replay(instance)
        assert answer["state"] == "accepted"
        assert answer["shadow_search_source_recall"] == 1.0
        assert answer["expected_ranges"] == answer["recalled_ranges"] == 1
        assert answer["unresolved_ranges"] == 0


def test_shadow_pages_through_every_cited_record(bank, tmp_path, monkeypatch):
    monkeypatch.setattr(cutover, "RECORD_PAGE", 1)
    routes = ConsumerRoutes(tmp_path / "routes.sqlite3")
    proof = routes.qualify(bank.tools, grant(bank.inputs), "keeper", questions(bank), 5000.0)
    assert (proof["state"], proof["recalled_ranges"]) == ("accepted", 1)
    routes.close()


def test_single_record_range_is_recalled(bank):
    asked = questions(bank)
    asked[0]["expected"][0]["last_sequence"] = 1
    assert shadow(bank.tools, grant(bank.inputs), asked, 5000.0)["state"] == "accepted"


def test_missing_expected_evidence_is_not_qualified(bank):
    asked = questions(bank)
    asked[0]["expected"][0]["last_sequence"] = 999
    answer = shadow(bank.tools, grant(bank.inputs), asked, 5000.0)
    assert answer["state"] == "rejected"
    assert answer["shadow_search_source_recall"] == 0.0


def test_search_without_expansion_is_rejected_without_source_mutation(bank, monkeypatch):
    before = bank.protected()
    original = records.call

    def without_expansion(tools, name, granted, request, now, embedder=None):
        if name == "session_records":
            return {"state": "rejected", "error": {"class": "evidence_unavailable"}}
        return original(tools, name, granted, request, now, embedder)

    monkeypatch.setattr(records, "call", without_expansion)
    answer = replay(bank)
    assert answer["state"] == "rejected" and answer["unresolved_ranges"] > 0
    assert bank.protected() == before


def test_unauthorized_expectation_cannot_be_recalled(bank):
    allowed = grant(bank.inputs, [bank.inputs["narrow_grant"]["projects"][0]])
    answer = shadow(bank.tools, allowed, questions(bank), 5000.0)
    assert answer["state"] == "rejected"
    assert answer["recalled_ranges"] == 0
    assert bank.uid("lock") not in json.dumps(answer)


@pytest.mark.parametrize(
    ("asked", "reason"),
    [
        ([], "at least one question"),
        ([{"id": "empty", "text": "anything", "expected": []}], "'empty' names no expected"),
    ],
)
def test_empty_baseline_cannot_qualify(bank, asked, reason):
    with pytest.raises(ValueError, match=reason):
        shadow(bank.tools, grant(bank.inputs), asked, 5000.0)


@pytest.mark.parametrize("bounds", [(5, 4), (0, 2), ("1", 4)])
def test_unordered_expected_range_cannot_qualify(bank, bounds):
    asked = questions(bank)
    asked[0]["expected"][0]["first_sequence"], asked[0]["expected"][0]["last_sequence"] = bounds
    with pytest.raises(ValueError, match="'catalog-lock' names a range that is not positive and ordered"):
        shadow(bank.tools, grant(bank.inputs), asked, 5000.0)


def test_expanded_records_outside_the_grant_are_counted_and_refuse(bank, monkeypatch):
    original, expansions = records.call, []

    def with_foreign(tools, name, granted, request, now, embedder=None):
        answer = original(tools, name, granted, request, now, embedder)
        if name == "session_records":
            expansions.append(name)
            foreign = {**answer["records"][0], "project_id": "github.com/elsewhere/other"}
            answer["records"] = [foreign, foreign, *answer["records"]]
        return answer

    monkeypatch.setattr(records, "call", with_foreign)
    answer = replay(bank)
    assert answer["state"] == "rejected"
    assert answer["unauthorized_results"] == 2 * len(expansions) > 0
    assert answer["recalled_ranges"] == answer["expected_ranges"] == 1


def test_failed_search_counts_as_unexpandable(bank, monkeypatch):
    def refused(tools, name, granted, request, now, embedder=None):
        return {"state": "rejected", "error": {"class": "dependency_unavailable"}}

    monkeypatch.setattr(records, "call", refused)
    answer = replay(bank)
    assert answer["state"] == "rejected"
    assert (answer["unexpandable_results"], answer["unauthorized_results"], answer["unresolved_ranges"]) == (1, 0, 1)


def test_route_requires_bound_qualification_and_compare_and_set(bank, tmp_path):
    routes = ConsumerRoutes(tmp_path / "routes.sqlite3")
    allowed = grant(bank.inputs)
    assert routes.route(allowed, "keeper")["endpoint"] == "local.v1"
    with pytest.raises(ValueError):
        routes.switch(allowed, "keeper", "session.v1", 0, "invented")
    proof = routes.qualify(bank.tools, allowed, "keeper", questions(bank), 5000.0)
    changed = routes.switch(allowed, "keeper", "session.v1", 0, proof["operation_id"])
    assert changed["revision"] == 1 and changed["endpoint"] == "session.v1"
    assert routes.switch(allowed, "keeper", "session.v1", 0, proof["operation_id"]) == changed
    with pytest.raises(ValueError):
        routes.switch(allowed, "other", "session.v1", 0, proof["operation_id"])
    with pytest.raises(ValueError):
        routes.switch(grant(bank.inputs, [bank.inputs["project"]]), "keeper", "session.v1", 0, proof["operation_id"])
    routes.close()


def test_route_rollback_survives_restart_and_capture_continues(bank, tmp_path):
    location = tmp_path / "routes.sqlite3"
    routes = ConsumerRoutes(location)
    allowed = grant(bank.inputs)
    proof = routes.qualify(bank.tools, allowed, "keeper", questions(bank), 5000.0)
    routes.switch(allowed, "keeper", "session.v1", 0, proof["operation_id"])
    routes.close()
    routes = ConsumerRoutes(location)
    session = bank.corpus.sessions["lock"]
    assert (
        session.append([[{"kind": "user_text", "text": "capture after read route rollback"}]])["state"] == "committed"
    )
    before = bank.protected()
    restored = routes.rollback(allowed, "keeper", 1)
    assert restored["endpoint"] == "local.v1" and restored["revision"] == 2
    assert bank.protected() == before
    assert routes.evidence(allowed, proof["operation_id"]) == proof
    assert routes.rollback(allowed, "keeper", 1) == restored
    with pytest.raises(ValueError):
        routes.switch(allowed, "keeper", "session.v1", 0, proof["operation_id"])
    assert replay(bank)["state"] == "accepted"
    routes.close()


def test_failed_qualification_keeps_last_route(bank, tmp_path):
    routes = ConsumerRoutes(tmp_path / "routes.sqlite3")
    allowed = grant(bank.inputs)
    asked = questions(bank)
    asked[0]["text"] = "no matching records"
    proof = routes.qualify(bank.tools, allowed, "keeper", asked, 5000.0)
    with pytest.raises(ValueError):
        routes.switch(allowed, "keeper", "session.v1", 0, proof["operation_id"])
    assert routes.route(allowed, "keeper")["revision"] == 0
    routes.close()


def test_consumer_dispatch_uses_versioned_route_and_expiring_aliases(bank, tmp_path):
    routes = ConsumerRoutes(tmp_path / "routes.sqlite3")
    allowed = grant(bank.inputs)
    proof = routes.qualify(bank.tools, allowed, "keeper", questions(bank), 5000.0)
    routes.switch(allowed, "keeper", "session.v1", 0, proof["operation_id"])
    request = {"schema_version": "1.0", "access_domain": allowed.corpus, "query": questions(bank)[0]["text"]}
    answer = routes.call({"session.v1": bank.tools}, allowed, "keeper", "session_search", request, 5000.0)
    assert answer["state"] == "answered"
    assert answer["results"]
    expired = routes.call(
        {"session.v1": bank.tools}, allowed, "keeper", "search_semantic", request, records.ALIAS_EXPIRES_AT
    )
    assert expired["state"] == "rejected"
    with pytest.raises(ValueError):
        routes.switch(allowed, "keeper", "unsupported.v2", 1, proof["operation_id"])
    routes.rollback(allowed, "keeper", 1)
    for served in ({"session.v1": bank.tools}, {"local.v1": bank.tools}):
        with pytest.raises(ValueError, match="routes to 'local.v1', which this caller does not serve"):
            routes.call(served, allowed, "keeper", "session_search", request, 5000.0)
    routes.close()


def test_rollback_restores_the_last_accepted_route_before_the_switch(bank, tmp_path):
    routes = ConsumerRoutes(tmp_path / "routes.sqlite3")
    allowed = grant(bank.inputs)
    with pytest.raises(ValueError, match="no switched route to roll back"):
        routes.rollback(allowed, "keeper", 0)
    proof = routes.qualify(bank.tools, allowed, "keeper", questions(bank), 5000.0)["operation_id"]
    routes.switch(allowed, "keeper", "session.v1", 0, proof)
    for stale in (0, 2):
        with pytest.raises(ValueError, match="the route changed since the given revision"):
            routes.rollback(allowed, "keeper", stale)
    assert routes.rollback(allowed, "keeper", 1)["endpoint"] == "local.v1"
    with pytest.raises(ValueError, match="no switched route to roll back"):
        routes.rollback(allowed, "keeper", 2)
    routes.switch(allowed, "keeper", "session.v1", 2, proof)
    routes.switch(allowed, "keeper", "local.v1", 3, proof)
    again = routes.rollback(allowed, "keeper", 4)
    assert (again["endpoint"], again["revision"], again["action"]) == ("session.v1", 5, "rollback")
    routes.close()


@pytest.mark.parametrize("version", ["2.0", None])
def test_dispatch_refuses_a_request_of_another_protocol(bank, tmp_path, version):
    routes = ConsumerRoutes(tmp_path / "routes.sqlite3")
    allowed = grant(bank.inputs)
    proof = routes.qualify(bank.tools, allowed, "keeper", questions(bank), 5000.0)
    routes.switch(allowed, "keeper", "session.v1", 0, proof["operation_id"])
    request = {"schema_version": version, "access_domain": allowed.corpus, "query": "locked"}
    with pytest.raises(ValueError, match="endpoint 'session.v1' accepts only protocol 1 requests"):
        routes.call({"session.v1": bank.tools}, allowed, "keeper", "session_search", request, 5000.0)
    routes.close()


@pytest.mark.parametrize("case", ["A", "B", "C"])
def test_package_cases_match_committed_evidence(tmp_path, case):
    observed = json.loads(json.dumps(cases.run_case(case, tmp_path)))
    path = EVIDENCE / f"{case.lower()}-result.json"
    expected = json.loads(path.read_text()) if path.exists() else None
    assert observed == expected, json.dumps(observed, sort_keys=True)


def test_package_record_and_manifest_match_the_fixture_inputs():
    record = json.loads((EVIDENCE / "result.json").read_text())
    found = json.loads((EVIDENCE / "manifest.json").read_text())
    inputs = (cases.FIXTURE, cases.QUESTIONS, cases.CASES_MODULE)
    expected = {
        path.relative_to(cases.CASES_MODULE.parent).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in inputs
    }
    assert found["input_sha256"] == expected
    assert found["fixture_inputs"] == record["fixture_inputs"] == cases.FIXTURE_INPUTS
    assert found["tested_commit"] == record["tested_commit"]
    assert record["evidence_class"] == cases.EVIDENCE_CLASS
