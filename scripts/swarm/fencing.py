"""One revisioned document per swarm holds every write the controller lease fences, so each commits whole or not at all."""

import copy
import json
import time
from collections.abc import Callable
from hashlib import sha256
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


def _project_executions(value: bytes) -> bytes:
    return _encode(json.loads(value)["executions"])


def _states(store: Any) -> Any:
    states = state.for_store(store, BUCKET)
    if isinstance(states, RedisStateStore):
        states.projection = _project_executions
    return states


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


def _decode(value: bytes) -> dict[str, Any]:
    return {**copy.deepcopy(EMPTY), **json.loads(value)}


def _archive_rows(store: Any, reference: str) -> dict[str, Any]:
    from scripts.swarm.store import SwarmError

    rows: dict[str, Any] = {}
    seen: set[str] = set()
    states = state.for_store(store, BUCKET)
    while reference:
        entry = states.get(reference)
        if entry is None:
            raise SwarmError("referenced execution archive is unavailable")
        chunk = json.loads(entry.value)
        for key, row in chunk["rows"].items():
            if key not in seen:
                rows[key] = row
        seen.update(chunk["rows"])
        seen.update(chunk["removed"])
        reference = chunk["parent"]
    return rows


def _save_archive_chunk(store: Any, slug: str, parent: str, rows: dict[str, Any], removed: list[str]) -> str:
    value = _encode({"parent": parent, "rows": rows, "removed": removed})
    reference = f"{slug}:archive:{sha256(value).hexdigest()}"
    states = state.for_store(store, BUCKET)
    try:
        states.create(reference, value)
    except StateConflict:
        entry = states.get(reference)
        if entry is None or entry.value != value:
            raise
    return reference


def hydrate_archive(store: Any, doc: dict[str, Any]) -> None:
    doc["archive"] = {**_archive_rows(store, doc.get("archive_ref", "")), **doc["archive"]}


def _externalize_archive(store: Any, slug: str, doc: dict[str, Any]) -> None:
    if "archive_ref" not in doc:
        return
    reference = doc["archive_ref"]
    previous = _archive_rows(store, reference)
    rows = doc["archive"]
    changed = [key for key, row in rows.items() if key not in previous or previous[key] != row]
    removed = sorted(previous.keys() - rows.keys())
    for start in range(0, max(len(changed), 1), 64):
        batch = {key: rows[key] for key in changed[start : start + 64]}
        if batch or removed:
            reference = _save_archive_chunk(store, slug, reference, batch, removed)
        removed = []
    doc["archive_ref"] = reference
    doc["archive"] = {}


def change_with_archive(store: Any, slug: str, doc: dict[str, Any], change: Callable[[dict[str, Any]], T]) -> T:
    hydrate_archive(store, doc)
    result = change(doc)
    _externalize_archive(store, slug, doc)
    return result


def migrate_archive(store: Any, slug: str, *, writers_upgraded: bool = False) -> None:
    """Enable external history only after every live writer has the dual reader."""
    from scripts.swarm.store import SwarmError

    if not writers_upgraded:
        raise SwarmError("upgrade every live fencing writer before migrating the archive")

    def migrate(doc: dict[str, Any]) -> None:
        hydrate_archive(store, doc)
        doc.setdefault("archive_ref", "")
        _externalize_archive(store, slug, doc)

    write(store, slug, migrate)


def read(store: Any, slug: str) -> dict[str, Any]:
    doc = _decode(_entry(_states(store), store, slug).value)
    hydrate_archive(store, doc)
    return doc


def read_executions(store: Any, slug: str) -> dict[str, Any]:
    states = _states(store)
    if isinstance(states, RedisStateStore):
        entry = states.get_projection(slug)
        if entry is not None:
            return json.loads(entry.value)
    return read(store, slug)["executions"]


LEASE_CACHE = "lease-cache"


def _lease_cache_key(store: Any, slug: str) -> str:
    return store.key(slug, LEASE_CACHE)


def _cache_lease(store: Any, states: Any, slug: str, doc: dict[str, Any], revision: int) -> None:
    if not isinstance(states, RedisStateStore):
        return
    payload = json.dumps(
        {"revision": revision, "lease": doc["lease"], "epoch": doc["epoch"], "legacy_owner": doc["legacy_owner"]}
    )
    store.redis.set(_lease_cache_key(store, slug), payload)


def read_lease(store: Any, slug: str) -> dict[str, Any] | None:
    states = _states(store)
    if not isinstance(states, RedisStateStore):
        return None
    raw = store.redis.get(_lease_cache_key(store, slug))
    revision = store.redis.get(states.revision_key)
    if raw is None or revision is None:
        return None
    cached = json.loads(raw)
    if str(cached["revision"]) != str(revision):
        return None
    return {"lease": cached["lease"]}


def write(store: Any, slug: str, change: Callable[[dict], T]) -> T:
    from scripts.swarm.store import SwarmError

    states = _states(store)
    for _ in range(ATTEMPTS):
        entry = _entry(states, store, slug)
        doc = _decode(entry.value)
        before = copy.deepcopy(doc)
        result = change(doc)
        if doc["archive"] and "archive_ref" in doc:
            hydrate_archive(store, doc)
            _externalize_archive(store, slug, doc)
        if doc == before:
            return result
        try:
            written = states.update(slug, _encode(doc), entry.revision)
        except StateConflict:
            continue
        _cache_lease(store, states, slug, doc, written)
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
        if "archive_ref" in doc:
            doc["archive_ref"] = ""
            _externalize_archive(store, slug, doc)

    write(store, slug, change)
