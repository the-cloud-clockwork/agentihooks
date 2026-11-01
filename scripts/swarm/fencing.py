"""One revisioned document per swarm holds every write the controller lease fences, so each commits whole or not at all."""

import copy
import json
import time
from collections.abc import Callable, Sequence
from contextvars import ContextVar
from hashlib import sha256
from secrets import randbelow
from typing import Any, TypeVar

from scripts.events import state
from scripts.events.bus import StateConflict
from scripts.events.redis_state import RedisStateStore
from scripts.tick.adapters.records_redis import LAYOUT, META, RedisRecords
from scripts.tick.ports.records import (
    BLIND,
    FencedBatch,
    LeaseFence,
    Record,
    Records,
    RecordWrite,
    RevisionConflict,
    StaleLease,
)

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
RECORDS = "records"
DOCUMENT = "document"
RECORDS_REF = "records:"
ARCHIVE = "archive"
ARCHIVE_REF = "archive_ref"
BY_NAME = "by-name"
SECTIONS = tuple(name for name, value in EMPTY.items() if isinstance(value, dict))
LOADED = (META, *(name for name in SECTIONS if name != ARCHIVE), BY_NAME)
STALE = "the controller lease is stale"
HYDRATED: ContextVar[dict[str, dict[str, Record]] | None] = ContextVar("fencing_hydrated", default=None)


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


def _migrated(store: Any, slug: str) -> RedisRecords | None:
    if state.backend() != "redis":
        return None
    port = RedisRecords(store.redis)
    return port if port.layout(slug) == RECORDS else None


def _assemble(slug: str, loaded: dict[str, dict[str, Record]]) -> dict[str, Any]:
    doc = copy.deepcopy(EMPTY)
    for name, rows in loaded.items():
        values = {key: found.value for key, found in rows.items()}
        if name == META:
            values.pop(LAYOUT, None)
            doc.update(values)
        elif name in SECTIONS:
            doc[name] = values
    doc[ARCHIVE_REF] = RECORDS_REF + slug
    return doc


def _heads(rows: dict[str, Any]) -> dict[str, str]:
    found: dict[str, tuple[Any, str]] = {}
    for key, row in rows.items():
        name, generation = row.get("name"), row.get("generation")
        if isinstance(name, str) and isinstance(generation, int | float):
            if name not in found or generation >= found[name][0]:
                found[name] = (generation, key)
    return {name: key for name, (_, key) in found.items()}


def _expected(loaded: dict[str, dict[str, Record]], section: str, key: str) -> int:
    if section == ARCHIVE:
        return BLIND
    found = loaded.get(section, {}).get(key)
    return 0 if found is None else found.revision


def _section_changes(
    section: str, before: dict[str, Any], after: dict[str, Any], loaded: dict[str, dict[str, Record]]
) -> list[RecordWrite]:
    writes = [
        RecordWrite("put", section, key, _expected(loaded, section, key), value)
        for key, value in after.items()
        if key not in before or before[key] != value
    ]
    removed = sorted(before.keys() - after.keys())
    return writes + [RecordWrite("delete", section, key, _expected(loaded, section, key)) for key in removed]


def _changes(
    before: dict[str, Any], after: dict[str, Any], loaded: dict[str, dict[str, Record]], guard_lease: bool
) -> list[RecordWrite]:
    """Every record a change wrote, each expecting the revision it was read at, plus the derived name index."""
    writes: list[RecordWrite] = []
    for name in sorted((before.keys() | after.keys()) - {ARCHIVE_REF}):
        if name in SECTIONS:
            writes += _section_changes(name, before.get(name, {}), after.get(name, {}), loaded)
        elif name not in after:
            writes.append(RecordWrite("delete", META, name, _expected(loaded, META, name)))
        elif name not in before or before[name] != after[name]:
            writes.append(RecordWrite("put", META, name, _expected(loaded, META, name), after[name]))
    heads = (_heads(before.get("executions", {})), _heads(after.get("executions", {})))
    writes += _section_changes(BY_NAME, heads[0], heads[1], loaded)
    if writes and guard_lease and not any(write.section == META and write.key == "lease" for write in writes):
        writes.append(RecordWrite("check", META, "lease", _expected(loaded, META, "lease")))
    return writes


def _lease_fence(fence: Any) -> LeaseFence | None:
    if fence is None:
        return None
    if isinstance(fence, int):
        return LeaseFence("", fence, now_ms())
    return LeaseFence(fence.owner, fence.epoch, now_ms())


def _retry(attempt: int) -> None:
    if attempt + 1 < ATTEMPTS:
        time.sleep((1 + randbelow(min(1 << attempt, 32))) / 1000)


def _write_records(store: Any, port: RedisRecords, slug: str, change: Callable[[dict[str, Any]], T], fence: Any) -> T:
    from scripts.swarm.store import SwarmError

    for attempt in range(ATTEMPTS):
        loaded = port.sections(slug, LOADED)
        doc = _assemble(slug, loaded)
        before = copy.deepcopy(doc)
        hydrated: dict[str, dict[str, Record]] = {}
        token = HYDRATED.set(hydrated)
        try:
            result = change(doc)
        finally:
            HYDRATED.reset(token)
        if ARCHIVE in hydrated:
            loaded[ARCHIVE] = hydrated[ARCHIVE]
            before[ARCHIVE] = {key: found.value for key, found in hydrated[ARCHIVE].items()}
        writes = _changes(before, doc, loaded, fence is None)
        if not writes:
            return result
        try:
            port.fenced_batch(slug, FencedBatch(tuple(writes), _lease_fence(fence)))
        except RevisionConflict:
            _retry(attempt)
            continue
        except StaleLease as exc:
            raise SwarmError(STALE) from exc
        return result
    raise SwarmError(CONTENTION)


def _record_archive(store: Any, slug: str) -> dict[str, Any]:
    rows = RedisRecords(store.redis).sections(slug, (ARCHIVE,))[ARCHIVE]
    hydrated = HYDRATED.get()
    if hydrated is not None:
        hydrated.setdefault(ARCHIVE, rows)
    return {key: copy.deepcopy(found.value) for key, found in rows.items()}


def _archived(store: Any, reference: str, execution_id: str) -> Any:
    if reference.startswith(RECORDS_REF):
        found = RedisRecords(store.redis).record(reference.removeprefix(RECORDS_REF), ARCHIVE, execution_id)
        return None if found is None else found.value
    return _archive_rows(store, reference).get(execution_id)


def _archive_rows(store: Any, reference: str) -> dict[str, Any]:
    from scripts.swarm.store import SwarmError

    if reference.startswith(RECORDS_REF):
        return _record_archive(store, reference.removeprefix(RECORDS_REF))
    rows: dict[str, Any] = {}
    seen: set[str] = set()
    states = state.for_store(store, BUCKET)
    while reference != "":
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


def execution_row(store: Any, doc: dict[str, Any], execution_id: str) -> dict[str, Any] | None:
    """Finds an execution in the current rows, then the inline archive, then the external archive history."""
    return (
        doc["executions"].get(execution_id)
        or doc["archive"].get(execution_id)
        or _archived(store, doc.get(ARCHIVE_REF, ""), execution_id)
    )


def _externalize_archive(store: Any, slug: str, doc: dict[str, Any]) -> None:
    if ARCHIVE_REF not in doc or doc[ARCHIVE_REF].startswith(RECORDS_REF):
        return
    reference = doc["archive_ref"]
    previous = _archive_rows(store, reference)
    rows = doc["archive"]
    changed = [key for key, row in rows.items() if key not in previous or previous[key] != row]
    removed = sorted(previous.keys() - rows.keys())
    if removed:
        reference = _save_archive_chunk(store, slug, reference, {}, removed)
    for start in range(0, len(changed), 64):
        batch = {key: rows[key] for key in changed[start : start + 64]}
        reference = _save_archive_chunk(store, slug, reference, batch, [])
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
    port = _migrated(store, slug)
    if port is not None:
        return _assemble(slug, port.sections(slug, (*LOADED, ARCHIVE)))
    doc = _decode(_entry(_states(store), store, slug).value)
    hydrate_archive(store, doc)
    return doc


def read_executions(store: Any, slug: str) -> dict[str, Any]:
    port = _migrated(store, slug)
    if port is not None:
        return _assemble(slug, port.sections(slug, ("executions",)))["executions"]
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
    port = _migrated(store, slug)
    if port is not None:
        found = port.record(slug, META, "lease")
        return {"lease": None if found is None else found.value}
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


def write(store: Any, slug: str, change: Callable[[dict[str, Any]], T], fence: Any = None) -> T:
    """Commits change whole or not at all; on per record keys the batch also checks fence, a held lease or epoch."""
    from scripts.swarm.store import SwarmError

    port = _migrated(store, slug)
    if port is not None:
        return _write_records(store, port, slug, change, fence)
    states = _states(store)
    for attempt in range(ATTEMPTS):
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
            _retry(attempt)
            continue
        _cache_lease(store, states, slug, doc, written)
        return result
    raise SwarmError(CONTENTION)


def authority_held(store: Any, slug: str, task: str) -> bool:
    port = _migrated(store, slug)
    if port is not None:
        return port.record(slug, "claims", task) is not None or port.record(slug, "journals", task) is not None
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


def watch(store: Any, pipe: Any, slug: str = "") -> None:
    """Makes a Redis transaction abort when any fencing document or the swarm's fenced records change."""
    states = _states(store)
    if isinstance(states, RedisStateStore):
        states.guard(pipe)
        if slug:
            pipe.watch(RedisRecords(store.redis).revision_key(slug))


def replace(store: Any, slug: str, saved: dict[str, Any] | None) -> None:
    """Puts back a saved document; a snapshot taken before the document existed is imported from its keys."""
    restored: dict[str, Any] = _legacy(store, slug) if saved is None else {**EMPTY, **saved}

    def change(doc: dict[str, Any]) -> Any:
        records = doc.get(ARCHIVE_REF, "").startswith(RECORDS_REF)
        if records:
            hydrate_archive(store, doc)
        doc.clear()
        doc.update(restored)
        if records:
            doc.pop(ARCHIVE_REF, None)
        elif ARCHIVE_REF in doc:
            doc[ARCHIVE_REF] = ""
            _externalize_archive(store, slug, doc)

    write(store, slug, change)


def _view(doc: dict[str, Any], section: str) -> dict[str, Any]:
    if section == META:
        return {key: value for key, value in doc.items() if key not in SECTIONS and key != ARCHIVE_REF}
    if section == BY_NAME:
        return _heads(doc["executions"])
    return doc[section]


def _admit(doc: dict[str, Any], revision: int, batch: FencedBatch) -> None:
    fence = batch.lease
    if fence is not None:
        live = doc["lease"]
        if (
            not live
            or (fence.owner and live["owner"] != fence.owner)
            or live["epoch"] != fence.epoch
            or live["expires_at"] <= fence.now_ms
        ):
            raise StaleLease(STALE)
    for write in batch.writes:
        found = revision if write.key in _view(doc, write.section) else 0
        if write.expected != BLIND and write.expected != found:
            raise RevisionConflict(f"fenced record {write.section}/{write.key} is not at revision {write.expected}")


def _apply(doc: dict[str, Any], write: RecordWrite) -> None:
    if write.kind == "check" or write.section == BY_NAME:
        return
    target = doc if write.section == META else doc[write.section]
    if write.kind == "put":
        target[write.key] = write.value
    else:
        target.pop(write.key, None)


class WholeDocumentRecords:
    """The records port over the whole fencing document, for bus deployments and swarms not yet migrated."""

    def __init__(self, store: Any) -> None:
        self.store = store

    def layout(self, slug: str) -> str:
        return DOCUMENT

    def sections(self, slug: str, names: Sequence[str]) -> dict[str, dict[str, Record]]:
        entry = _entry(_states(self.store), self.store, slug)
        doc = _decode(entry.value)
        if ARCHIVE in names:
            hydrate_archive(self.store, doc)
        return {name: {key: Record(entry.revision, value) for key, value in _view(doc, name).items()} for name in names}

    def record(self, slug: str, section: str, key: str) -> Record | None:
        return self.sections(slug, (section,))[section].get(key)

    def fenced_batch(self, slug: str, batch: FencedBatch) -> int:
        """Checks the lease fence and every expected revision against the document, then writes it whole."""
        states = _states(self.store)
        entry = _entry(states, self.store, slug)
        doc = _decode(entry.value)
        if any(write.section == ARCHIVE for write in batch.writes):
            hydrate_archive(self.store, doc)
        _admit(doc, entry.revision, batch)
        for write in batch.writes:
            _apply(doc, write)
        _externalize_archive(self.store, slug, doc)
        try:
            written = states.update(slug, _encode(doc), entry.revision)
        except StateConflict as exc:
            raise RevisionConflict(str(exc)) from exc
        _cache_lease(self.store, states, slug, doc, written)
        return written


def records(store: Any, slug: str) -> Records:
    """The swarm's fenced records: per record Redis keys once migrated, else the whole document behind the same port."""
    return _migrated(store, slug) or WholeDocumentRecords(store)


def migrate_records(store: Any, slug: str, *, writers_upgraded: bool = False) -> int:
    """Copies the whole fencing document into per record keys once every live fencing writer reads both layouts."""
    from redis.exceptions import WatchError

    from scripts.swarm.store import SwarmError

    if not writers_upgraded:
        raise SwarmError("upgrade every live fencing writer before migrating to per record keys")
    if state.backend() != "redis":
        raise SwarmError("per record fencing keys need the Redis state backend")
    port = RedisRecords(store.redis)
    states = RedisStateStore(store.redis, BUCKET)
    for _ in range(ATTEMPTS):
        if port.layout(slug) == RECORDS:
            return 0
        with store.redis.pipeline() as pipe:
            try:
                pipe.watch(states.watch_key, states.revision_key, port.revision_key(slug))
                doc = _decode(_entry(states, store, slug).value)
                hydrate_archive(store, doc)
                writes = _changes(copy.deepcopy(EMPTY), doc, {}, False)
                writes.append(RecordWrite("put", META, LAYOUT, 0, RECORDS))
                pipe.multi()
                port.queue_batch(pipe, slug, FencedBatch(tuple(writes)))
                pipe.execute()
                return len(writes)
            except WatchError:
                continue
    raise SwarmError(CONTENTION)
