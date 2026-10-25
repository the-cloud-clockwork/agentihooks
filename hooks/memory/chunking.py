"""Search chunks are projections of committed events; their ids come from source sequences and the chunker version."""

import hashlib
import json
from dataclasses import asdict, dataclass

from hooks.memory import archive
from hooks.memory.api.ingest import Grant
from hooks.memory.catalog import COUNT, COUNTER, Catalog, Conflict

NARRATIVE = {"user_text": "user", "assistant_text": "assistant", "system_text": "system", "turn_complete": "summary"}
TOOL_KINDS = ("tool_call", "tool_result")
TURN_START = "user_text"
EVIDENCE_KEYS = ("event_id", "sequence", "byte_start", "byte_end")
REBUILT = "chunks_rebuilt_without_content_change"
BUILT, VALIDATED = "built", "validated"
CHUNK_SCHEMA = (
    """CREATE TABLE IF NOT EXISTS chunk_generation (
        access_domain TEXT NOT NULL, session_uid TEXT NOT NULL, chunker_version TEXT NOT NULL, policy TEXT NOT NULL,
        state TEXT NOT NULL, built_through TEXT NOT NULL,
        PRIMARY KEY (access_domain, session_uid, chunker_version))""",
    """CREATE TABLE IF NOT EXISTS search_chunk (
        access_domain TEXT NOT NULL, session_uid TEXT NOT NULL, chunker_version TEXT NOT NULL, chunk_id TEXT NOT NULL,
        source_generation TEXT NOT NULL, first_sequence INTEGER NOT NULL, record TEXT NOT NULL,
        PRIMARY KEY (access_domain, session_uid, chunker_version, chunk_id))""",
    """CREATE TABLE IF NOT EXISTS chunk_cutover (
        access_domain TEXT NOT NULL, session_uid TEXT NOT NULL, chunker_version TEXT NOT NULL,
        revision INTEGER NOT NULL, PRIMARY KEY (access_domain, session_uid))""",
)
CHUNK_TABLES = ("chunk_generation", "search_chunk", "chunk_cutover")
GENERATION_KEY = "access_domain = ? AND session_uid = ? AND chunker_version = ?"
GENERATION = f"SELECT policy, state, built_through FROM chunk_generation WHERE {GENERATION_KEY}"
SAVE_GENERATION = (
    "INSERT INTO chunk_generation VALUES (?, ?, ?, ?, ?, ?) "
    "ON CONFLICT (access_domain, session_uid, chunker_version) "
    "DO UPDATE SET state = excluded.state, built_through = excluded.built_through"
)
SET_GENERATION_STATE = f"UPDATE chunk_generation SET state = ? WHERE {GENERATION_KEY} AND built_through = ?"
CHUNKS = (
    f"SELECT chunk_id, record FROM search_chunk WHERE {GENERATION_KEY} "
    "ORDER BY source_generation, first_sequence, chunk_id"
)
INSERT_CHUNK = "INSERT INTO search_chunk VALUES (?, ?, ?, ?, ?, ?, ?)"
DELETE_CHUNK = f"DELETE FROM search_chunk WHERE {GENERATION_KEY} AND chunk_id = ?"
CUTOVER = "SELECT chunker_version, revision FROM chunk_cutover WHERE access_domain = ? AND session_uid = ?"
SAVE_CUTOVER = (
    "INSERT INTO chunk_cutover VALUES (?, ?, ?, ?) ON CONFLICT (access_domain, session_uid) "
    "DO UPDATE SET chunker_version = excluded.chunker_version, revision = excluded.revision"
)
BAD_POLICY = "a chunker policy needs a version and a positive character bound"
POLICY_CONFLICT = "this chunker version was built with another policy; a policy change needs a new version"
STALE_BUILD = "the build read fewer events than the stored generation; nothing changed"
CHUNK_CONFLICT = "a chunk with this id was stored with different content"
UNKNOWN_GENERATION = "no chunk generation exists for this session and version"
NOT_VALIDATED = "only a validated chunk generation can become active"
STALE_CUTOVER = "the cutover revision is stale; nothing changed"
UNSUPPORTED = "a chunk names a source range the catalog does not hold; the generation was not validated"
REBUILT_DURING_VALIDATION = "the generation was rebuilt while it was validated; validate it again"


@dataclass(frozen=True)
class Policy:
    version: str
    max_chars: int


DEFAULT_POLICY = Policy("turn-v1", 2000)


@dataclass(frozen=True)
class ChunkScope:
    access_domain: str
    session_uid: str
    parent_session_uid: str | None = None


def _digest(value: object) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def _text(value: object) -> str:
    return value if isinstance(value, str) else json.dumps(value, sort_keys=True, ensure_ascii=False)


def _line(record: dict) -> str:
    kind = record["kind"]
    if kind == "tool_call":
        return f"tool {record.get('tool_name', '')}: {_text(record.get('tool_input'))}"
    if kind == "tool_result":
        label = "result error" if record.get("is_error") else "result"
        return f"{label}: {_text(record.get('tool_result'))}"
    return f"{NARRATIVE[kind]}: {record.get('text', '')}"


def _turns(events: list[dict]) -> list[list[dict]]:
    turns = []
    for event in events:
        if not turns or event["record"]["kind"] == TURN_START:
            turns.append([])
        turns[-1].append(event)
    return turns


def _parts(events: list[dict], limit: int) -> list[list[dict]]:
    parts, size = [], 0
    for event in events:
        length = len(_line(event["record"]))
        if parts and size + 1 + length <= limit:
            parts[-1].append(event)
            size += 1 + length
        else:
            parts.append([event])
            size = length
    return parts


def _tools(events: list[dict]) -> list[list[dict]]:
    pairs = {}
    for event in events:
        record = event["record"]
        if record["kind"] in TOOL_KINDS:
            pairs.setdefault(record.get("tool_use_id") or f"#{event['sequence']}", []).append(event)
    return list(pairs.values())


def _tool(events: list[dict]) -> dict:
    by_kind = {event["record"]["kind"]: event for event in events}
    call, result = by_kind.get("tool_call"), by_kind.get("tool_result")
    record = events[0]["record"]
    return {
        "tool_use_id": record.get("tool_use_id") or "",
        "tool_name": call["record"].get("tool_name", "") if call else None,
        "is_error": bool(result["record"].get("is_error")) if result else None,
        "call_event_id": call["event_id"] if call else None,
        "result_event_id": result["event_id"] if result else None,
    }


@dataclass(frozen=True)
class _Unit:
    policy: Policy
    scope: ChunkScope
    generation: str
    turn_id: str


def _chunk(unit: _Unit, kind: str, events: list[dict], tool: dict | None) -> dict:
    text = "\n".join(_line(event["record"]) for event in events)
    preview = text[: unit.policy.max_chars]
    sequences = [event["sequence"] for event in events]
    scope = unit.scope
    identity = [unit.policy.version, scope.access_domain, scope.session_uid, unit.generation, kind, sequences]
    return {
        "chunk_id": f"chunk-{_digest(identity)[:40]}",
        "chunker_version": unit.policy.version,
        "access_domain": scope.access_domain,
        "session_uid": scope.session_uid,
        "parent_session_uid": scope.parent_session_uid,
        "source_generation": unit.generation,
        "turn_id": unit.turn_id,
        "kind": kind,
        "project_ids": sorted({event["project_id"] for event in events if event["project_id"]}),
        "preview": preview,
        "truncated": len(text) > len(preview),
        "omitted_chars": len(text) - len(preview),
        "content_hash": hashlib.sha256(preview.encode()).hexdigest(),
        "tool": tool,
        "evidence": [{key: event[key] for key in EVIDENCE_KEYS} for event in events],
    }


def chunk_events(policy: Policy, scope: ChunkScope, generation: str, events: list[dict]) -> list[dict]:
    if not policy.version or policy.max_chars < 1:
        raise ValueError(BAD_POLICY)
    chunks = []
    for turn in _turns(sorted(events, key=lambda event: event["sequence"])):
        identity = [policy.version, scope.access_domain, scope.session_uid, generation, turn[0]["sequence"]]
        unit = _Unit(policy, scope, generation, f"turn-{_digest(identity)[:40]}")
        narrative = [event for event in turn if event["record"]["kind"] in NARRATIVE]
        chunks += [_chunk(unit, "turn", part, None) for part in _parts(narrative, policy.max_chars)]
        chunks += [_chunk(unit, "tool", pair, _tool(pair)) for pair in _tools(turn)]
    return sorted(chunks, key=lambda chunk: chunk["evidence"][0]["sequence"])


def locator(chunk: dict) -> list[dict]:
    return [
        {
            "access_domain": chunk["access_domain"],
            "session_uid": chunk["session_uid"],
            "source_generation": chunk["source_generation"],
            "byte_start": item["byte_start"],
            "byte_end": item["byte_end"],
        }
        for item in chunk["evidence"]
    ]


def _same_content(chunk: dict) -> str:
    return _digest([chunk["kind"], chunk["content_hash"], chunk["evidence"]])


class ChunkIndex:
    def __init__(self, catalog: Catalog) -> None:
        self.catalog = catalog
        for statement in CHUNK_SCHEMA:
            catalog.db.execute(statement)

    def _generation(self, key: tuple[str, str, str]) -> dict | None:
        row = self.catalog.db.execute(GENERATION, key).fetchone()
        if row is None:
            return None
        return {"policy": json.loads(row[0]), "state": row[1], "built_through": json.loads(row[2])}

    def _stored(self, key: tuple[str, str, str]) -> dict[str, dict]:
        return {row[0]: json.loads(row[1]) for row in self.catalog.db.execute(CHUNKS, key).fetchall()}

    def _write(self, key: tuple[str, str, str], chunks: list[dict], through: dict, policy: Policy) -> dict:
        stored = self._stored(key)
        fresh = {chunk["chunk_id"]: chunk for chunk in chunks}
        if any(chunk_id in stored and stored[chunk_id] != chunk for chunk_id, chunk in fresh.items()):
            raise Conflict(CHUNK_CONFLICT)
        inserted = [chunk for chunk_id, chunk in fresh.items() if chunk_id not in stored]
        superseded = [chunk_id for chunk_id in stored if chunk_id not in fresh]
        before = {_same_content(stored[chunk_id]) for chunk_id in superseded}
        rebuilt = sum(_same_content(chunk) in before for chunk in inserted)
        for chunk_id in superseded:
            self.catalog.db.execute(DELETE_CHUNK, (*key, chunk_id))
        for chunk in inserted:
            first = chunk["evidence"][0]["sequence"]
            row = (*key, chunk["chunk_id"], chunk["source_generation"], first, json.dumps(chunk, ensure_ascii=False))
            self.catalog.db.execute(INSERT_CHUNK, row)
        found = self._generation(key)
        state = found["state"] if found and not inserted and not superseded else BUILT
        self.catalog.db.execute(SAVE_GENERATION, (*key, json.dumps(asdict(policy)), state, json.dumps(through)))
        self.catalog.db.execute(COUNT, (key[0], REBUILT, rebuilt))
        return {
            "state": "committed",
            "generation_state": state,
            "inserted": [chunk["chunk_id"] for chunk in inserted],
            "superseded": superseded,
            "unchanged": len(fresh) - len(inserted),
            REBUILT: rebuilt,
        }

    def build(self, grant: Grant, scope: ChunkScope, policy: Policy = DEFAULT_POLICY) -> dict:
        _, sources = archive._readable(self.catalog, grant, scope.access_domain, scope.session_uid)
        chunks = [
            chunk
            for source in sources
            for chunk in chunk_events(policy, scope, source["source_generation"], source["events"])
        ]
        through = {
            source["source_generation"]: max((event["sequence"] for event in source["events"]), default=0)
            for source in sources
        }
        key = (scope.access_domain, scope.session_uid, policy.version)
        with self.catalog._transaction():
            found = self._generation(key)
            if found and found["policy"] != asdict(policy):
                raise Conflict(POLICY_CONFLICT)
            if found and any(through.get(name, -1) < sequence for name, sequence in found["built_through"].items()):
                raise Conflict(STALE_BUILD)
            result = self._write(key, chunks, through, policy)
        return {**result, "chunker_version": policy.version, "built_through": through}

    def chunks(self, grant: Grant, domain: str, session_uid: str, version: str) -> list[dict]:
        archive._readable(self.catalog, grant, domain, session_uid)
        if self._generation((domain, session_uid, version)) is None:
            raise Conflict(UNKNOWN_GENERATION)
        return list(self._stored((domain, session_uid, version)).values())

    def validate(self, store: archive.ObjectStore, grant: Grant, domain: str, session_uid: str, version: str) -> dict:
        key = (domain, session_uid, version)
        found = self._generation(key)
        chunks = self.chunks(grant, domain, session_uid, version)
        evidence = archive.evidence(self.catalog, store, grant, domain, session_uid)
        held = {
            (source["source_generation"], *(event[name] for name in EVIDENCE_KEYS))
            for source in evidence["sources"]
            for event in source["events"]
        }
        for chunk in chunks:
            for item in chunk["evidence"]:
                if (chunk["source_generation"], *(item[name] for name in EVIDENCE_KEYS)) not in held:
                    raise archive.EvidenceIntegrityError(UNSUPPORTED)
        through = json.dumps(found["built_through"])
        if self.catalog.db.execute(SET_GENERATION_STATE, (VALIDATED, *key, through)).rowcount != 1:
            raise Conflict(REBUILT_DURING_VALIDATION)
        return {"chunker_version": version, "state": VALIDATED, "chunks": len(chunks)}

    def cutover(self, domain: str, session_uid: str) -> dict:
        row = self.catalog.db.execute(CUTOVER, (domain, session_uid)).fetchone()
        return {"chunker_version": row[0], "revision": row[1]} if row else {"chunker_version": None, "revision": 0}

    def activate(self, domain: str, session_uid: str, version: str, revision: int) -> int:
        with self.catalog._transaction():
            found = self._generation((domain, session_uid, version))
            if found is None or found["state"] != VALIDATED:
                raise Conflict(NOT_VALIDATED)
            if self.cutover(domain, session_uid)["revision"] != revision:
                raise Conflict(STALE_CUTOVER)
            self.catalog.db.execute(SAVE_CUTOVER, (domain, session_uid, version, revision + 1))
            return revision + 1

    def active(self, grant: Grant, domain: str, session_uid: str) -> dict:
        archive._readable(self.catalog, grant, domain, session_uid)
        current = self.cutover(domain, session_uid)
        version = current["chunker_version"]
        chunks = list(self._stored((domain, session_uid, version)).values()) if version else []
        return {**current, "chunks": chunks}

    def rebuilt_total(self, domain: str) -> int:
        row = self.catalog.db.execute(COUNTER, (domain, REBUILT)).fetchone()
        return row[0] if row else 0

    def snapshot(self) -> dict[str, list]:
        return {table: sorted(self.catalog.db.execute(f"SELECT * FROM {table}").fetchall()) for table in CHUNK_TABLES}
