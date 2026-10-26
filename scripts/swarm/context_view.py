"""The ledger page's brain context projection: the pack each task was given, from which brain, and the evidence behind
each item. The page only displays it; expanding an item reads the stored evidence again under the task brain's scope,
so a copied link or a cached page grants nothing."""

import json
from collections.abc import Mapping

from scripts.swarm.store import RedisStore

PACKS = "task-context-packs"
EVIDENCE = "context-evidence"
FAILURES = "context-expand-failures"
METRIC = "ledger_evidence_expand_failures"
SWITCH = "AGENTIHOOKS_CONTEXT_INSPECTION"
SWARM_BRAIN = "swarm:"
TRUST = {"archive": "transcript evidence", "synthesized": "brain synthesis", "inferred": "inferred intent"}
FRESH, EXPIRED, NO_RECEIPT = "fresh", "expired", "no receipt"
SUPERSEDED, REVOKED = "superseded", "revoked"
OUTSIDE = "private to another brain, outside this viewer's scope"
NOT_IN_PACK = "this reference is not part of the task's context pack"
GONE = "the source record is no longer held"
OFF = "brain context inspection is off"
NOT_SWARM = "a ledger records only packs from a swarm brain"


class Refused(Exception):
    def __init__(self, status: int, reason: str, text: str) -> None:
        super().__init__(text)
        self.status, self.reason = status, reason


def enabled(environ: Mapping[str, str]) -> bool:
    return environ.get(SWITCH) != "off"


def record(store: RedisStore, slug: str, task: str, pack: dict) -> None:
    if not pack["brain"].startswith(SWARM_BRAIN):
        raise ValueError(NOT_SWARM)
    store.redis.hset(store.key(slug, PACKS), task, json.dumps(pack, sort_keys=True))


def _held(pipe, key: str, ref: str) -> dict | None:
    raw = pipe.hget(key, ref)
    return json.loads(raw) if raw else None


def put_evidence(store: RedisStore, slug: str, ref: str, evidence: dict) -> bool:
    """A revocation is terminal: a late or replayed write of the source never brings its content back."""
    key = store.key(slug, EVIDENCE)

    def write(pipe) -> bool:
        held = _held(pipe, key, ref)
        if held and held["state"] == REVOKED:
            return False
        pipe.multi()
        pipe.hset(key, ref, json.dumps({**evidence, "ref": ref}, sort_keys=True))
        return True

    return store.redis.transaction(write, key, value_from_callable=True)


def revoke(store: RedisStore, slug: str, ref: str, reason: str) -> None:
    """The tombstone keeps only what names the source, so no copy of its content outlives the revocation."""
    key = store.key(slug, EVIDENCE)

    def write(pipe) -> None:
        held = _held(pipe, key, ref) or {}
        tombstone = {"ref": ref, "brain": held.get("brain", ""), "kind": held.get("kind", ""), "state": REVOKED}
        pipe.multi()
        pipe.hset(key, ref, json.dumps({**tombstone, "reason": reason}, sort_keys=True))

    store.redis.transaction(write, key)


def failures(store: RedisStore, slug: str) -> dict[str, int]:
    return {reason: int(count) for reason, count in store.redis.hgetall(store.key(slug, FAILURES)).items()}


def freshness(receipt: dict | None, now_ms: int) -> str:
    if not receipt:
        return NO_RECEIPT
    return FRESH if now_ms < receipt["expires_at_ms"] else EXPIRED


def _labels(item: dict) -> list[str]:
    labels = [TRUST[item["trust"]]]
    if item["state"] == SUPERSEDED:
        labels.append(f"superseded by {item['superseded_by']}")
    return labels


def item_view(item: dict, brain: str, held: dict | None) -> dict:
    if item["brain"] != brain:
        return {
            "ref": "",
            "kind": item["kind"],
            "labels": [TRUST[item["trust"]]],
            "summary": "",
            "unavailable": OUTSIDE,
        }
    shown = {"ref": item["ref"], "kind": item["kind"], "labels": _labels(item), "summary": "", "unavailable": ""}
    if held is None or held["state"] == REVOKED:
        return {**shown, "unavailable": held["reason"] if held else GONE}
    return {**shown, "summary": item["summary"]}


def card(task: str, pack: dict, evidence: dict[str, str], now_ms: int) -> dict:
    receipt = pack["receipt"]
    held = {ref: json.loads(raw) for ref, raw in evidence.items()}
    return {
        "task": task,
        "brain": pack["brain"],
        "purpose": pack["purpose"],
        "receipt": receipt["receipt_id"] if receipt else "",
        "receipt_state": freshness(receipt, now_ms),
        "delivery": [f"{adapter} {outcome}" for adapter, outcome in sorted(pack["delivery"].items())],
        "items": [item_view(item, pack["brain"], held.get(item["ref"])) for item in pack["items"]],
        "omitted": len(pack["omitted"]),
    }


def view(store: RedisStore, slug: str, now_ms: int) -> dict:
    packs = store.redis.hgetall(store.key(slug, PACKS))
    evidence = store.redis.hgetall(store.key(slug, EVIDENCE))
    cards = [card(task, json.loads(raw), evidence, now_ms) for task, raw in sorted(packs.items())]
    return {"cards": cards, METRIC: failures(store, slug)}


def _resolve(store: RedisStore, slug: str, task: str, ref: str) -> dict:
    raw = store.redis.hget(store.key(slug, PACKS), task)
    pack = json.loads(raw) if raw else None
    item = next((item for item in pack["items"] if item["ref"] == ref), None) if pack else None
    if item is None:
        raise Refused(404, "not_in_pack", NOT_IN_PACK)
    held = _held(store.redis, store.key(slug, EVIDENCE), ref)
    if not pack["brain"].startswith(SWARM_BRAIN) or item["brain"] != pack["brain"]:
        raise Refused(403, "forbidden_scope", OUTSIDE)
    if held is None:
        raise Refused(410, "unavailable", GONE)
    if held["state"] == REVOKED:
        raise Refused(410, REVOKED, held["reason"])
    if held["brain"] != pack["brain"]:
        raise Refused(403, "forbidden_scope", OUTSIDE)
    return {**held, "task": task, "labels": _labels(item)}


def expand(store: RedisStore, slug: str, task: str, ref: str) -> dict:
    """The supporting record range or lesson revision behind one item of the task's pack, read under its brain."""
    try:
        return _resolve(store, slug, task, ref)
    except Refused as refused:
        store.redis.hincrby(store.key(slug, FAILURES), refused.reason, 1)
        raise
