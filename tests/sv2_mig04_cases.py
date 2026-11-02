"""SV2-MIG-04 acceptance cases: shadow search qualification and consumer cutover over the session tools fixture."""

import json
from pathlib import Path

from hooks.memory import archive
from hooks.memory.api import records
from hooks.memory.api.ingest import SCHEMA_VERSION
from hooks.memory.cutover import LOCAL, SESSION, ConsumerRoutes, shadow
from tests.sv2_idx04_cases import grant
from tests.sv2_idx05_cases import Bank

CASES_MODULE = Path(__file__)
FIXTURE = CASES_MODULE.parent / "fixtures/swarm_v2/session-tools.json"
QUESTIONS = CASES_MODULE.parent / "fixtures/swarm_v2/cutover-questions.json"
CONSUMER = "memory-keeper"
EVIDENCE_CLASS = (
    "mocked: questions reconstructed from repository history over the sanitized session tools fixture, served by "
    "temporary SQLite catalogs and object stores; no live transcript, hosted catalog or deployed consumer was used"
)
FIXTURE_INPUTS = {
    "questions": "catalog lock incident and empty kernel feed, each with a hand checked expected source range",
    "grants": "the fixture grant over both projects, and a narrow grant over the kernel project only",
    "unexpandable": "the same catalog served with an empty object store, so search answers and expansion is refused",
    "capture": "a new turn appended to the lock session while the consumer route is switched and rolled back",
}


def bank(root: Path, native: str | None = None) -> Bank:
    inputs = json.loads(FIXTURE.read_text())
    if native:
        for item in inputs["sessions"]:
            item["session_uid"] = f"{item['session_uid'].rsplit(':', 1)[0]}:{native}-{item['name']}"
    return Bank(inputs, root)


def questions(found: Bank) -> list[dict]:
    asked = json.loads(QUESTIONS.read_text())["questions"]
    by_name = {item["name"]: item for item in found.inputs["sessions"]}
    return [
        {
            "id": item["id"],
            "text": item["text"],
            "expected": [
                {
                    "session_uid": by_name[item["session"]]["session_uid"],
                    "source_generation": by_name[item["session"]]["source_generation"],
                    "first_sequence": first,
                    "last_sequence": last,
                }
                for first, last in item["ranges"]
            ],
        }
        for item in asked
    ]


def now(found: Bank) -> float:
    return found.inputs["clock"]["now"]


def request(found: Bank, text: str) -> dict:
    return {"schema_version": "1.0", "access_domain": found.inputs["grant"]["corpus"], "query": text}


def _refused(action) -> str:
    try:
        action()
    except ValueError as error:
        return str(error)
    return "accepted"


def _dispatch(found: Bank, routes: ConsumerRoutes, name: str, at: float) -> dict:
    answer = routes.call({SESSION: found.tools}, grant(found.inputs), CONSUMER, name, request(found, "locked"), at)
    hits = [found.short(item["session_uid"]) for item in answer.get("results", [])]
    return {"state": answer["state"], "error": answer.get("error", {}).get("class"), "sessions": sorted(set(hits))}


def case_a(root: Path) -> dict:
    runs = {}
    for name, native in (("first", None), ("second", "independent")):
        found = bank(root / name, native)
        allowed = grant(found.inputs)
        routes = ConsumerRoutes(root / name / "routes.sqlite3")
        proof = routes.qualify(found.tools, allowed, CONSUMER, questions(found), now(found))
        switched = routes.switch(allowed, CONSUMER, SESSION, 0, proof["operation_id"])
        runs[name] = {
            "proof": proof,
            "route": switched,
            "dispatch": _dispatch(found, routes, "session_search", now(found)),
        }
        routes.close()
    outcomes = [
        {**run, "proof": {**run["proof"], "operation_id": None}, "route": {**run["route"], "operation_id": None}}
        for run in runs.values()
    ]
    return {"case": "A", "independent_outcomes_equal": outcomes[0] == outcomes[1], "runs": runs}


def case_b(root: Path) -> dict:
    found = bank(root / "bank")
    allowed, asked, at = grant(found.inputs), questions(found), now(found)
    before = found.protected()
    blind = records.SessionTools(found.corpus.catalog, archive.ObjectStore(root / "empty-objects"))
    unexpandable = shadow(blind, allowed, asked, at)
    narrow = grant(found.inputs, [found.inputs["narrow_grant"]["projects"][0]])
    lock_uid = found.uid("lock")
    unauthorized = shadow(found.tools, narrow, asked[:1], at)
    routes = ConsumerRoutes(root / "routes.sqlite3")
    proof = routes.qualify(blind, allowed, CONSUMER, asked, at)
    good = routes.qualify(found.tools, allowed, CONSUMER, asked, at)
    refused = {
        "unexpandable_proof": _refused(lambda: routes.switch(allowed, CONSUMER, SESSION, 0, proof["operation_id"])),
        "unknown_proof": _refused(lambda: routes.switch(allowed, CONSUMER, SESSION, 0, "invented")),
        "other_grant": _refused(lambda: routes.switch(narrow, CONSUMER, SESSION, 0, good["operation_id"])),
        "other_consumer": _refused(lambda: routes.switch(allowed, "other", SESSION, 0, good["operation_id"])),
        "unsupported_endpoint": _refused(
            lambda: routes.switch(allowed, CONSUMER, "session.v2", 0, good["operation_id"])
        ),
        "empty_baseline": _refused(lambda: shadow(found.tools, allowed, [], at)),
    }
    route = routes.route(allowed, CONSUMER)
    routes.switch(allowed, CONSUMER, SESSION, 0, good["operation_id"])
    expired = _dispatch(found, routes, "search_semantic", records.ALIAS_EXPIRES_AT)
    routes.close()
    return {
        "case": "B",
        "unexpandable": unexpandable,
        "unauthorized": unauthorized,
        "unauthorized_discloses_session": lock_uid in json.dumps(unauthorized),
        "refused": refused,
        "route_after_refusals": route,
        "expired_alias": expired,
        "protected_unchanged": found.protected() == before,
    }


def case_c(root: Path) -> dict:
    found = bank(root / "bank")
    allowed, asked, at = grant(found.inputs), questions(found), now(found)
    location = root / "routes.sqlite3"
    routes = ConsumerRoutes(location)
    proof = routes.qualify(found.tools, allowed, CONSUMER, asked, at)
    routes.switch(allowed, CONSUMER, SESSION, 0, proof["operation_id"])
    routes.close()
    routes = ConsumerRoutes(location)
    session = found.corpus.sessions["lock"]
    domain, uid = found.inputs["grant"]["corpus"], found.uid("lock")
    generation = found.inputs["sessions"][0]["source_generation"]
    held = len(found.corpus.catalog.events(domain, uid, generation))
    captured = session.append([[{"kind": "user_text", "text": "capture while the consumer route rolls back"}]])
    before = found.protected()
    restored = routes.rollback(allowed, CONSUMER, 1)
    replayed = routes.rollback(allowed, CONSUMER, 1)
    result = {
        "case": "C",
        "capture": captured["state"],
        "events_before_capture": held,
        "events_after_capture": len(found.corpus.catalog.events(domain, uid, generation)),
        "restored": restored,
        "restored_endpoint_is_local": restored["endpoint"] == LOCAL,
        "rollback_replay_equal": replayed == restored,
        "protected_unchanged": found.protected() == before,
        "evidence_kept": routes.evidence(allowed, proof["operation_id"]) == proof,
        "stale_switch": _refused(lambda: routes.switch(allowed, CONSUMER, SESSION, 0, proof["operation_id"])),
        "local_dispatch": _refused(lambda: _dispatch(found, routes, "session_search", at)),
        "shadow_after_rollback": shadow(found.tools, allowed, asked, at)["state"],
    }
    routes.close()
    return result


CASES = {"A": case_a, "B": case_b, "C": case_c}


def run_case(case: str, root: Path) -> dict:
    return {"schema_version": SCHEMA_VERSION, **CASES[case](root)}
