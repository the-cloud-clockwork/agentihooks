"""One revisioned document per swarm holds every write the controller lease fences, so each commits whole or not at all."""

import copy
import json
import time
from collections.abc import Callable
from typing import Any, TypeVar

from scripts.events import state
from scripts.events.bus import StateConflict
from scripts.events.redis_state import RedisStateStore

BUCKET = "fencing"
COUNTERS = "counters"
IDENTITY_CONFLICTS = "identity-conflicts"
STALE_REJECTIONS = "stale-generation-rejections"
ATTEMPTS = 16
CONTENTION = "the swarm fencing state kept changing; nothing was committed"
EMPTY: dict[str, Any] = {
    "lease": None,
    "legacy_owner": "",
    "epoch": 0,
    "leader_changes": 0,
    "intents": {},
    "orphans": {},
    "executions": {},
    "archive": {},
    "grants": {},
    "registrations": {},
    "grants_disabled": "",
    "claims": {},
    "journals": {},
    "fences": {},
    "operations": {},
}
HASHES = {
    "executions": "executions",
    "archive": "executions-archive",
    "intents": "controller-intents",
    "grants": "launch-grants",
    "registrations": "launch-registrations",
    "operations": "runtime-operations",
}
LEGACY_OWNER = "control-owner"
LEGACY_COUNTERS = {"epoch": "control-epoch", "leader_changes": "controller-leader-changes"}
LEGACY_CLAIMS = "task-authority"
LEGACY_FENCES = "heartbeat-sequence"
LEGACY_JOURNALS = "claim-journal"
LEGACY_DISABLED = "launch-grants-disabled"
LEGACY_ORPHANS = "controller-orphans"
T = TypeVar("T")


def unavailable() -> tuple[type[BaseException], ...]:
    from scripts.swarm.store import store_errors

    return (*store_errors(), *state.unavailable())


def now_ms() -> int:
    return round(time.time() * 1000)


def _states(store: Any) -> Any:
    return state.for_store(store, BUCKET)


def _task(key: str) -> str:
    return key.rsplit(":", 1)[1]


def _scan(store: Any, slug: str, part: str) -> Any:
    return store.redis.scan_iter(match=store.key(slug, part, "*"))


def _values(store: Any, slug: str, part: str) -> Any:
    for key in _scan(store, slug, part):
        if found := store.redis.get(key):
            yield key, found


def _lists(store: Any, slug: str, part: str) -> Any:
    for key in _scan(store, slug, part):
        if found := store.redis.lrange(key, 0, -1):
            yield key, found


def _legacy(store: Any, slug: str) -> dict:
    doc = {**EMPTY}
    raw = store.redis.get(store.key(slug, LEGACY_OWNER))
    if raw and raw.startswith("{"):
        doc["lease"] = json.loads(raw)
    elif raw:
        doc["legacy_owner"] = raw
    for field, part in LEGACY_COUNTERS.items():
        doc[field] = int(store.redis.get(store.key(slug, part)) or 0)
    for section, part in HASHES.items():
        doc[section] = {key: json.loads(raw) for key, raw in store.redis.hgetall(store.key(slug, part)).items()}
    doc["claims"] = {_task(key): json.loads(raw) for key, raw in _values(store, slug, LEGACY_CLAIMS)}
    doc["fences"] = {key: json.loads(raw) for key, raw in _values(store, slug, LEGACY_FENCES)}
    doc["journals"] = {
        _task(key): [json.loads(row) for row in rows] for key, rows in _lists(store, slug, LEGACY_JOURNALS)
    }
    doc["grants_disabled"] = store.redis.get(store.key(slug, LEGACY_DISABLED)) or ""
    doc["orphans"] = {key: int(value) for key, value in store.redis.hgetall(store.key(slug, LEGACY_ORPHANS)).items()}
    return doc


def _encode(doc: dict) -> bytes:
    return json.dumps(doc, separators=(",", ":")).encode()


def _entry(states: Any, store: Any, slug: str) -> Any:
    entry = states.get(slug)
    if entry is not None:
        return entry
    try:
        states.create(slug, _encode(_legacy(store, slug)))
    except StateConflict:
        pass
    return states.get(slug)


def _decode(value: bytes) -> dict:
    return {**copy.deepcopy(EMPTY), **json.loads(value)}


def read(store: Any, slug: str) -> dict:
    return _decode(_entry(_states(store), store, slug).value)


def write(store: Any, slug: str, change: Callable[[dict], T]) -> T:
    from scripts.swarm.store import SwarmError

    states = _states(store)
    for _ in range(ATTEMPTS):
        entry = _entry(states, store, slug)
        doc = _decode(entry.value)
        result = change(doc)
        if doc == _decode(entry.value):
            return result
        try:
            states.update(slug, _encode(doc), entry.revision)
        except StateConflict:
            continue
        return result
    raise SwarmError(CONTENTION)


def authority_held(store: Any, slug: str, task: str) -> bool:
    doc = read(store, slug)
    return task in doc["claims"] or task in doc["journals"]


def count(store: Any, slug: str, part: str) -> int:
    entry = state.for_store(store, COUNTERS).get(f"{slug}.{part}")
    return int(store.redis.get(store.key(slug, part)) or 0) if entry is None else int(entry.value)


def bump(store: Any, slug: str, part: str) -> None:
    """Counts a refused write outside the document, so a refusal leaves every fenced row untouched."""
    from scripts.swarm.store import SwarmError

    counters = state.for_store(store, COUNTERS)
    key = f"{slug}.{part}"
    for _ in range(ATTEMPTS):
        entry = counters.get(key)
        try:
            if entry is None:
                counters.create(key, str(count(store, slug, part) + 1).encode())
            else:
                counters.update(key, str(int(entry.value) + 1).encode(), entry.revision)
        except StateConflict:
            continue
        return
    raise SwarmError(CONTENTION)


def watch(store: Any, pipe: Any) -> None:
    """Makes a Redis transaction abort when any fencing document changes; other backends have nothing to watch."""
    states = _states(store)
    if isinstance(states, RedisStateStore):
        states.guard(pipe)


def replace(store: Any, slug: str, saved: dict | None) -> None:
    """Puts back a saved document; a snapshot taken before the document existed is imported from its keys."""
    restored = _legacy(store, slug) if saved is None else {**EMPTY, **saved}

    def change(doc: dict[str, Any]) -> Any:
        doc.clear()
        doc.update(restored)

    write(store, slug, change)
