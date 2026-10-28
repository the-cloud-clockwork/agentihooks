import copy
import json
from pathlib import Path

import pytest
from hooks.memory.cutover import ConsumerRoutes, shadow

from hooks.memory.api import records
from tests.sv2_idx04_cases import grant
from tests.sv2_idx05_cases import Bank

pytestmark = pytest.mark.unit
FIXTURE = Path(__file__).parent / "fixtures/swarm_v2/session-tools.json"


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
    second = Bank(copy.deepcopy(bank.inputs), tmp_path / "independent")
    for instance in (bank, second):
        answer = replay(instance)
        assert answer["state"] == "accepted"
        assert answer["shadow_search_source_recall"] == 1.0
        assert answer["expected_ranges"] == answer["recalled_ranges"] == 1
        assert answer["unresolved_ranges"] == 0


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


@pytest.mark.parametrize("asked", [[], [{"id": "empty", "text": "anything", "expected": []}]])
def test_empty_baseline_cannot_qualify(bank, asked):
    with pytest.raises(ValueError):
        shadow(bank.tools, grant(bank.inputs), asked, 5000.0)


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
    with pytest.raises(ValueError):
        routes.call({"session.v1": bank.tools}, allowed, "keeper", "session_search", request, 5000.0)
    routes.close()
