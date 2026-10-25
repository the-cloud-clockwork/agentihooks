import hashlib
import json
import tempfile
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator
from referencing import Registry, Resource

from hooks.memory import archive, chunking
from hooks.memory import catalog as catalog_module
from hooks.memory.api.ingest import SCHEMA_VERSION, Grant, ingest
from hooks.memory.catalog import Catalog, Conflict

FIXTURE = Path(__file__).parent / "fixtures/swarm_v2/session-chunks.json"
SCHEMAS = Path(__file__).parents[1] / "docs/swarm-v2/schemas"
EVIDENCE_CLASS = (
    "mocked: an in-process caller, a SQLite catalog file and a local content addressed object folder stand in for "
    "the session service, the central database and the archive bucket; no embedding model, HTTP server or Pod runs"
)


def schema_validator():
    common, chunk = (json.loads((SCHEMAS / name).read_text(encoding="utf-8")) for name in ("common.json", "chunk.json"))
    registry = Registry().with_resources(
        [(common["$id"], Resource.from_contents(common)), (chunk["$id"], Resource.from_contents(chunk))]
    )
    return Draft202012Validator(chunk, registry=registry)


def grant(inputs, corpus=None):
    data = inputs["grant"]
    return Grant(data["principal"], corpus or data["corpus"], frozenset(data["projects"]))


def scope(inputs):
    return chunking.ChunkScope(inputs["grant"]["corpus"], inputs["session_uid"], inputs["parent_session_uid"])


def policy(inputs, name):
    return chunking.Policy(**inputs["policies"][name])


def records(turns):
    built = []
    for turn in turns:
        for item in turn:
            record = {key: value for key, value in item.items() if key != "repeat"}
            if "repeat" in item:
                record["tool_result"] = " ".join([item["tool_result"]] * item["repeat"])
            built.append(record)
    return built


def line(record):
    return json.dumps(record, ensure_ascii=False).encode() + b"\n"


class Session:
    def __init__(self, inputs, root):
        self.inputs, self.path = inputs, root / "central/catalog.sqlite3"
        self.store = archive.ObjectStore(root / "central/objects")
        self.open()
        self.catalog.register(
            catalog_module.SessionSource(
                inputs["grant"]["corpus"],
                inputs["session_uid"],
                inputs["source_generation"],
                inputs["provider"],
                inputs["origin"],
                inputs["retention"],
            )
        )
        self.sequence, self.cursor, self.batches = 0, 0, 0

    def open(self):
        self.catalog = Catalog(self.path)
        self.index = chunking.ChunkIndex(self.catalog)

    def restart(self):
        self.catalog.close()
        self.open()

    def append(self, turns):
        events, data, start = [], b"", self.cursor
        for record in records(turns):
            encoded = line(record)
            self.sequence += 1
            project = None if record["kind"] == "tool_call" else self.inputs["grant"]["projects"][0]
            end = self.cursor + len(encoded)
            item = {"sequence": self.sequence, "byte_start": self.cursor, "byte_end": end, "project_id": project}
            events.append({**item, "record": record})
            data, self.cursor = data + encoded, end
        domain, uid, generation = self.inputs["grant"]["corpus"], self.inputs["session_uid"], self.generation
        entry = catalog_module.ArchiveEntry(domain, uid, generation, start, self.cursor, self.store.put(data))
        archive.acknowledge(self.catalog, self.store, entry)
        self.batches += 1
        body = {
            "schema_version": SCHEMA_VERSION,
            "batch_id": f"capture:{self.batches}",
            "access_domain": domain,
            "session_uid": uid,
            "source_generation": generation,
            "events": events,
        }
        return ingest(self.catalog, grant(self.inputs), body)

    @property
    def generation(self):
        return self.inputs["source_generation"]

    def build(self, name="current"):
        return self.index.build(grant(self.inputs), scope(self.inputs), policy(self.inputs, name))

    def validate(self, name="current"):
        domain, uid = self.inputs["grant"]["corpus"], self.inputs["session_uid"]
        return self.index.validate(self.store, grant(self.inputs), domain, uid, policy(self.inputs, name).version)

    def activate(self, name, revision):
        domain, uid = self.inputs["grant"]["corpus"], self.inputs["session_uid"]
        return self.index.activate(domain, uid, policy(self.inputs, name).version, revision)

    def active(self):
        return self.index.active(grant(self.inputs), self.inputs["grant"]["corpus"], self.inputs["session_uid"])

    def chunks(self, name):
        domain, uid = self.inputs["grant"]["corpus"], self.inputs["session_uid"]
        return self.index.chunks(grant(self.inputs), domain, uid, policy(self.inputs, name).version)

    def full(self, chunk):
        return b"".join(
            archive.source_range(self.catalog, self.store, grant(self.inputs), ref) for ref in chunking.locator(chunk)
        )

    def protected(self):
        kept = {table: rows for table, rows in self.catalog.snapshot().items() if table != "ingest_counter"}
        return {**kept, **self.index.snapshot()}

    def metrics(self):
        domain = self.inputs["grant"]["corpus"]
        return {"access_domain": domain, chunking.REBUILT: self.index.rebuilt_total(domain)}


def shape(chunks):
    return [
        {
            "chunk_id": chunk["chunk_id"],
            "kind": chunk["kind"],
            "turn_id": chunk["turn_id"],
            "sequences": [item["sequence"] for item in chunk["evidence"]],
            "preview_chars": len(chunk["preview"]),
            "truncated": chunk["truncated"],
            "omitted_chars": chunk["omitted_chars"],
            "content_hash": chunk["content_hash"],
            "tool": chunk["tool"],
        }
        for chunk in chunks
    ]


def refusal(call):
    try:
        call()
    except (Conflict, ValueError, archive.EvidenceIntegrityError, archive.EvidenceRefused) as error:
        return {"error": type(error).__name__, "detail": str(error)}
    return {"error": None}


def history(inputs):
    appended = inputs["appended_turns"]
    return inputs["turns"][:-appended], inputs["turns"][-appended:]


def positive(inputs, root):
    earlier, appended = history(inputs)
    session = Session(inputs, root)
    session.append(earlier)
    first = session.build()
    session.validate()
    session.activate("current", 0)
    before = session.active()["chunks"]
    session.append(appended)
    grown = session.build()
    after = session.active()["chunks"]
    by_id = {chunk["chunk_id"]: chunk for chunk in after}
    assert all(by_id.get(chunk["chunk_id"]) == chunk for chunk in before)
    assert grown["superseded"] == [] and grown[chunking.REBUILT] == 0
    assert sorted(grown["inserted"]) == sorted(chunk["chunk_id"] for chunk in after if chunk not in before)
    validator = schema_validator()
    for chunk in after:
        validator.validate(chunk)
        assert chunk["parent_session_uid"] == inputs["parent_session_uid"]
        assert session.full(chunk) == b"".join(line(record) for record in sequence_records(session, chunk))
    turn_one = [chunk for chunk in after if chunk["turn_id"] == after[0]["turn_id"]]
    tools = [chunk["tool"] for chunk in turn_one if chunk["kind"] == "tool"]
    assert [tool["tool_use_id"] for tool in tools] == ["toolu_ls", "toolu_read"]
    assert all(tool["call_event_id"] and tool["result_event_id"] for tool in tools)
    return {
        "first_build": {key: first[key] for key in ("state", "generation_state", "unchanged", chunking.REBUILT)},
        "first_build_inserted": len(first["inserted"]),
        "appended_build": {key: grown[key] for key in ("state", "superseded", "unchanged", chunking.REBUILT)},
        "appended_build_inserted": len(grown["inserted"]),
        "earlier_chunks_unchanged": True,
        "multi_tool_turn": tools,
        "schema_valid": True,
        "every_locator_exact": True,
        "chunks": shape(after),
        "metrics": session.metrics(),
    }


def sequence_records(session, chunk):
    events = session.catalog.events(chunk["access_domain"], chunk["session_uid"], chunk["source_generation"])
    wanted = {item["sequence"] for item in chunk["evidence"]}
    return [event["record"] for event in events if event["sequence"] in wanted]


def rejection(inputs, root):
    session = Session(inputs, root)
    session.append(inputs["turns"])
    session.build()
    long = next(chunk for chunk in session.chunks("current") if chunk["truncated"])
    bound = inputs["policies"]["current"]["max_chars"]
    assert len(long["preview"]) == bound and long["omitted_chars"] > 0
    result = json.loads(session.full(long).splitlines()[-1])
    assert result["tool_result"] == records(inputs["turns"])[result_index(inputs)]["tool_result"]
    domain, uid = inputs["grant"]["corpus"], inputs["session_uid"]
    digest = session.catalog.manifest(domain, uid, session.generation)[0]["object_digest"]
    lost = session.store.root / digest[:2] / digest
    kept = lost.read_bytes()
    before = session.protected()
    changed = chunking.Policy(inputs["policies"]["current"]["version"], bound + 1)
    attempts = {
        "foreign_corpus": refusal(
            lambda: session.index.build(grant(inputs, inputs["foreign_corpus"]), scope(inputs), changed)
        ),
        "policy_changed_under_same_version": refusal(
            lambda: session.index.build(grant(inputs), scope(inputs), changed)
        ),
        "unbounded_policy": refusal(
            lambda: session.index.build(grant(inputs), scope(inputs), chunking.Policy("turn-v0", 0))
        ),
        "activate_unvalidated": refusal(lambda: session.activate("current", 0)),
    }
    lost.unlink()
    attempts["validate_missing_archive_object"] = refusal(session.validate)
    assert session.protected() == before
    assert all(item["error"] for item in attempts.values())
    diagnostics = json.dumps(attempts, ensure_ascii=False)
    for hidden in ("build log", "personal-corpus", "worker-a", "café"):
        assert hidden not in diagnostics
    session.store.put(kept)
    corrected = session.validate()
    return {
        "long_tool_chunk": shape([long])[0],
        "long_result_retrievable_whole": True,
        "rejected": attempts,
        "protected_state_unchanged": True,
        "corrected_after_object_restored": corrected,
        "metrics": session.metrics(),
    }


def result_index(inputs):
    flat = records(inputs["turns"])
    return max(range(len(flat)), key=lambda index: len(str(flat[index].get("tool_result", ""))))


def recovery(inputs, root):
    earlier, appended = history(inputs)
    session = Session(inputs, root)
    session.append(earlier)
    session.build()
    session.validate()
    revision = session.activate("current", 0)
    stale_found = archive._readable(session.catalog, grant(inputs), inputs["grant"]["corpus"], inputs["session_uid"])
    session.append(appended)
    session.build()
    current = session.chunks("current")
    staged = session.build("next")
    still_active = session.active()
    assert still_active["chunker_version"] == inputs["policies"]["current"]["version"]
    assert still_active["chunks"] == current
    session.restart()
    survived = session.index.cutover(inputs["grant"]["corpus"], inputs["session_uid"])
    replayed = session.build("next")
    before = session.protected()
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(archive, "_readable", lambda *_: stale_found)
        stale = refusal(lambda: session.build("next"))
    assert session.protected() == before and stale["error"] == "Conflict"
    session.validate("next")
    revision = session.activate("next", revision)
    cut = session.active()
    events_before = session.catalog.events(inputs["grant"]["corpus"], inputs["session_uid"], session.generation)
    session.validate()
    stale_cutover = refusal(lambda: session.activate("current", 1))
    revision = session.activate("current", revision)
    rolled = session.active()
    events_after = session.catalog.events(inputs["grant"]["corpus"], inputs["session_uid"], session.generation)
    assert rolled["chunks"] == current and events_before == events_after
    assert {chunk["chunk_id"] for chunk in cut["chunks"]}.isdisjoint(chunk["chunk_id"] for chunk in current)
    assert replayed["inserted"] == [] and replayed["superseded"] == []
    return {
        "staged_generation": {key: staged[key] for key in ("chunker_version", "generation_state", "unchanged")},
        "staged_inserted": len(staged["inserted"]),
        "active_before_cutover": still_active["chunker_version"],
        "cutover_survived_restart": survived,
        "replayed_build": {key: replayed[key] for key in ("inserted", "superseded", "unchanged")},
        "stale_build": stale,
        "cutover": {
            "chunker_version": cut["chunker_version"],
            "revision": cut["revision"],
            "chunks": shape(cut["chunks"]),
        },
        "stale_cutover": stale_cutover,
        "rollback_rehearsal": {
            "restored": rolled["chunker_version"],
            "revision": revision,
            "chunks_match_prior_generation": True,
            "source_event_ids_unchanged": True,
        },
        "metrics": session.metrics(),
    }


CASES = {"a": positive, "b": rejection, "c": recovery}


def run_case(case):
    inputs = json.loads(FIXTURE.read_text(encoding="utf-8"))
    with tempfile.TemporaryDirectory() as folder:
        observed = CASES[case](inputs, Path(folder))
    return {
        "case": f"T-SV2-IDX-02-{case.upper()}",
        "state": "passed",
        "evidence_class": EVIDENCE_CLASS,
        "input_sha256": {FIXTURE.name: hashlib.sha256(FIXTURE.read_bytes()).hexdigest()},
        "observed": observed,
    }
