import hashlib
import json
from pathlib import Path

from scripts.swarm import context_view as cv
from scripts.swarm.store import RedisStore

FIXTURES = Path(__file__).parent / "fixtures" / "swarm_v2"
INPUTS = ("context-evidence.json",)
SLUG = "fixture"
CASES = {"a": "T-SV2-UX-04-A", "b": "T-SV2-UX-04-B", "c": "T-SV2-UX-04-C"}
EVIDENCE_CLASS = (
    "mocked: a fakeredis swarm store holding the fixture's delivered pack and its evidence records, read through the "
    "ledger page projection and the evidence expand path a ledger API read calls; no ledger HTTP server, browser, "
    "brain or cluster runs"
)


def fixture() -> dict:
    return json.loads((FIXTURES / "context-evidence.json").read_text())


def world(server=None) -> tuple[RedisStore, dict]:
    import fakeredis

    data = fixture()
    store = RedisStore(fakeredis.FakeRedis(server=server or fakeredis.FakeServer(), decode_responses=True))
    cv.record(store, SLUG, data["task"], data["pack"])
    for ref, evidence in data["evidence"].items():
        cv.put_evidence(store, SLUG, ref, evidence)
    return store, data


def protected(store: RedisStore) -> dict:
    return {name: store.redis.hgetall(store.key(SLUG, name)) for name in (cv.PACKS, cv.EVIDENCE)}


def outcome(store: RedisStore, task: str, ref: str) -> dict:
    try:
        found = cv.expand(store, SLUG, task, ref)
        return {
            "state": "expanded",
            "range": [found.get("first"), found.get("last")],
            "revision": found.get("revision"),
        }
    except cv.Refused as refused:
        return {"state": "refused", "status": refused.status, "reason": refused.reason, "text": str(refused)}


def case_a() -> dict:
    found = {}
    for run in ("first", "second"):
        store, data = world()
        [card] = cv.view(store, SLUG, data["now_ms"])["cards"]
        items = {item["ref"] or "private": item["labels"] for item in card["items"]}
        expanded = {ref: outcome(store, data["task"], ref) for ref in ("session:s-41", "decision:queue-v1")}
        found[run] = {"receipt": card["receipt_state"], "labels": items, "expanded": expanded}
        found[run][cv.METRIC] = cv.failures(store, SLUG)
    checks = {
        "independent_runs_agree": found["first"] == found["second"],
        "record_range_expands": found["first"]["expanded"]["session:s-41"]["range"] == [120, 124],
        "lesson_revision_expands": found["first"]["expanded"]["decision:queue-v1"]["revision"] == 1,
        "inferred_intent_labelled": found["first"]["labels"]["lesson:operator-intent-7"] == ["inferred intent"],
        "superseded_labelled": "superseded by decision:queue-v2" in found["first"]["labels"]["decision:queue-v1"],
        "no_failures": found["first"][cv.METRIC] == {},
    }
    return {"observed": found["first"], "checks": checks}


def case_b() -> dict:
    store, data = world()
    before = protected(store)
    refused = {ref: outcome(store, data["task"], ref) for ref in ("session:p-7", "arc:elsewhere")}
    private = json.dumps(refused) + json.dumps(cv.view(store, SLUG, data["now_ms"]))
    checks = {
        "personal_link_refused": refused["session:p-7"]["reason"] == "forbidden_scope",
        "foreign_ref_refused": refused["arc:elsewhere"]["reason"] == "not_in_pack",
        "state_unchanged": protected(store) == before,
        "nothing_disclosed": "private planning notes" not in private and "personal session" not in private,
        "a_new_valid_request_succeeds": outcome(store, data["task"], "arc:merge-queue")["state"] == "expanded",
    }
    return {"observed": {"refused": refused, cv.METRIC: cv.failures(store, SLUG)}, "checks": checks}


def case_c() -> dict:
    import fakeredis

    server = fakeredis.FakeServer()
    store, data = world(server)
    opened = outcome(store, data["task"], "session:s-41")
    reason = data["revoked"]["session:s-41"]
    cv.revoke(store, SLUG, "session:s-41", reason)
    tombstone = store.redis.hget(store.key(SLUG, cv.EVIDENCE), "session:s-41")
    restarted = RedisStore(fakeredis.FakeRedis(server=server, decode_responses=True))
    cv.revoke(restarted, SLUG, "session:s-41", reason)
    replayed = cv.put_evidence(restarted, SLUG, "session:s-41", data["evidence"]["session:s-41"])
    after = outcome(restarted, data["task"], "session:s-41")
    item = cv.view(restarted, SLUG, data["now_ms"])["cards"][0]["items"][3]
    held = restarted.redis.hget(restarted.key(SLUG, cv.EVIDENCE), "session:s-41")
    checks = {
        "opened_before_revocation": opened["state"] == "expanded",
        "unavailable_with_explanation": after
        == {"state": "refused", "status": 410, "reason": "revoked", "text": reason},
        "page_shows_unavailable": (item["summary"], item["unavailable"]) == ("", reason),
        "replayed_revoke_is_idempotent": held == tombstone,
        "stale_write_cannot_restore": replayed is False and "records" not in json.loads(held),
    }
    return {"observed": {"after": after, "item": item, cv.METRIC: cv.failures(restarted, SLUG)}, "checks": checks}


def run_case(case: str) -> dict:
    result = {"a": case_a, "b": case_b, "c": case_c}[case]()
    digests = {name: hashlib.sha256((FIXTURES / name).read_bytes()).hexdigest() for name in INPUTS}
    state = "passed" if all(result["checks"].values()) else "failed"
    return {"case": CASES[case], "evidence_class": EVIDENCE_CLASS, "input_sha256": digests, "state": state, **result}
