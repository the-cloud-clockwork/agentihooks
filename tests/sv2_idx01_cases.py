import hashlib
import json
import shutil
import tempfile
from pathlib import Path

import pytest

from hooks.memory import archive
from hooks.memory import catalog as catalog_module
from hooks.memory.api.ingest import SCHEMA_VERSION, Grant, ingest
from hooks.memory.catalog import Catalog, Conflict

FIXTURE = Path(__file__).parent / "fixtures/swarm_v2/session-archive.json"
EVIDENCE_CLASS = (
    "mocked: an in-process caller, a SQLite catalog file and a local content addressed object folder stand in for "
    "the session service, the central database and the archive bucket; no HTTP server, Pod or worker runs"
)


ROLLBACK_TABLES = ("session_record", "session_source", "archive_manifest", "session_event")


def grant(inputs):
    data = inputs["grant"]
    return Grant(data["principal"], data["corpus"], frozenset(data["projects"]))


def lines(inputs):
    built = []
    for _ in range(inputs["rounds"]):
        for turn in inputs["turns"]:
            record = {key: value for key, value in turn.items() if key != "repeat"}
            if "repeat" in turn:
                record["text"] = " ".join([turn["text"]] * turn["repeat"])
            built.append((record, json.dumps(record, ensure_ascii=False).encode() + b"\n"))
    return built


def events(inputs):
    built, cursor = [], 0
    for sequence, (record, line) in enumerate(lines(inputs), start=1):
        project = None if record["kind"] == "tool_call" else inputs["grant"]["projects"][0]
        built.append(
            {
                "sequence": sequence,
                "byte_start": cursor,
                "byte_end": cursor + len(line),
                "project_id": project,
                "record": record,
            }
        )
        cursor += len(line)
    return built


def transcript(inputs):
    return b"".join(line for _, line in lines(inputs))


def request(inputs, generation):
    return {
        "schema_version": SCHEMA_VERSION,
        "batch_id": f"capture:{generation['source_generation']}",
        "access_domain": inputs["grant"]["corpus"],
        "session_uid": inputs["session_uid"],
        "source_generation": generation["source_generation"],
        "events": events(inputs),
    }


def source(inputs, generation):
    return catalog_module.SessionSource(
        inputs["grant"]["corpus"],
        inputs["session_uid"],
        generation["source_generation"],
        inputs["provider"],
        generation["origin"],
        inputs["retention"],
    )


def capture(inputs, catalog, store, worker):
    acknowledged = []
    data = transcript(inputs)
    for generation in inputs["generations"]:
        path = worker / f"{generation['source_generation']}.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        catalog.register(source(inputs, generation))
        split = events(inputs)[generation["split_after_line"] - 1]["byte_end"]
        for start, end in ((0, split), (split, len(data))):
            digest = store.put(path.read_bytes()[start:end])
            entry = catalog_module.ArchiveEntry(
                inputs["grant"]["corpus"], inputs["session_uid"], generation["source_generation"], start, end, digest
            )
            archive.acknowledge(catalog, store, entry)
        acknowledged.append(ingest(catalog, grant(inputs), request(inputs, generation)))
    return acknowledged


def references(inputs, generation):
    return [
        {
            "access_domain": inputs["grant"]["corpus"],
            "session_uid": inputs["session_uid"],
            "source_generation": generation["source_generation"],
            "byte_start": event["byte_start"],
            "byte_end": event["byte_end"],
        }
        for event in events(inputs)
    ]


def ranges_match(inputs, catalog, store):
    lines_by_range = [line for _, line in lines(inputs)]
    for generation in inputs["generations"]:
        read = [archive.source_range(catalog, store, grant(inputs), ref) for ref in references(inputs, generation)]
        if read != lines_by_range:
            return False
    return True


def metrics(catalog, inputs):
    domain = inputs["grant"]["corpus"]
    return {
        "access_domain": domain,
        "archive_integrity_failures_total": catalog.integrity_failures_total(domain),
        "ingest_duplicate_events_total": catalog.duplicate_events_total(domain),
    }


def shape(found):
    return {
        "complete": found["complete"],
        "state": found["state"],
        "revision": found["revision"],
        "sources": [
            {
                "source_generation": item["source_generation"],
                "events": len(item["events"]),
                "archive": item["archive"],
            }
            for item in found["sources"]
        ],
    }


def positive(inputs, root):
    catalog, store = Catalog(root / "central/catalog.sqlite3"), archive.ObjectStore(root / "central/objects")
    acknowledged = capture(inputs, catalog, store, root / "worker")
    shutil.rmtree(root / "worker")
    found = archive.evidence(catalog, store, grant(inputs), inputs["grant"]["corpus"], inputs["session_uid"])
    text = sum(len(event["record"].get("text", "")) for item in found["sources"] for event in item["events"])
    shown = archive.preview(found)
    assert text > archive.LEGACY_PREVIEW_CHARS
    assert found["complete"] is True and shown["complete"] is False
    assert [event["record"] for event in found["sources"][0]["events"]] == [record for record, _ in lines(inputs)]
    assert len(shown["text"]) == archive.LEGACY_PREVIEW_CHARS and shown["omitted_chars"] > 0
    assert ranges_match(inputs, catalog, store)
    return {
        "acknowledged": [item["state"] for item in acknowledged],
        "worker_folder_deleted": not (root / "worker").exists(),
        "evidence": shape(found),
        "text_chars": text,
        "legacy_preview_chars": archive.LEGACY_PREVIEW_CHARS,
        "preview": {key: shown[key] for key in ("complete", "omitted_chars", "session_uid")},
        "every_source_range_exact": True,
        "metrics": metrics(catalog, inputs),
    }


def protected(catalog):
    return {table: rows for table, rows in catalog.snapshot().items() if table != "ingest_counter"}


def refusal(call):
    try:
        call()
    except (archive.EvidenceIntegrityError, archive.EvidenceRefused) as error:
        return {"error": type(error).__name__, "detail": str(error)}
    return {"error": None}


def rejection(inputs, root):
    catalog, store = Catalog(root / "central/catalog.sqlite3"), archive.ObjectStore(root / "central/objects")
    capture(inputs, catalog, store, root / "worker")
    domain, uid = inputs["grant"]["corpus"], inputs["session_uid"]
    missing = catalog.manifest(domain, uid, inputs["generations"][inputs["missing_generation"]]["source_generation"])
    lost = store.root / missing[-1]["object_digest"][:2] / missing[-1]["object_digest"]
    kept = lost.read_bytes()
    lost.unlink()
    before = protected(catalog)
    whole = refusal(lambda: archive.evidence(catalog, store, grant(inputs), domain, uid))
    tail = references(inputs, inputs["generations"][inputs["missing_generation"]])[-1]
    ranged = refusal(lambda: archive.source_range(catalog, store, grant(inputs), tail))
    foreign = refusal(lambda: archive.evidence(catalog, store, grant(inputs), inputs["foreign_corpus"], uid))
    assert protected(catalog) == before
    assert whole["error"] == "EvidenceIntegrityError" and ranged["error"] == "EvidenceIntegrityError"
    diagnostics = json.dumps([whole, ranged, foreign], ensure_ascii=False)
    for hidden in ("long transcript", "personal-corpus", "worker-a"):
        assert hidden not in diagnostics
    failures = catalog.integrity_failures_total(domain)
    intact = ranges_match({**inputs, "generations": inputs["generations"][:1]}, catalog, store)
    store.put(kept)
    corrected = archive.evidence(catalog, store, grant(inputs), domain, uid)
    return {
        "missing_object_evidence": whole,
        "missing_object_range": ranged,
        "foreign_corpus": foreign,
        "protected_state_unchanged": True,
        "failures_after_refusals": failures,
        "intact_generation_ranges_exact": intact,
        "corrected_after_object_restored": shape(corrected),
        "metrics": metrics(catalog, inputs),
    }


def recovery(inputs, root):
    path, store = root / "central/catalog.sqlite3", archive.ObjectStore(root / "central/objects")
    first = Catalog(path)
    capture(inputs, first, store, root / "worker")
    domain, uid = inputs["grant"]["corpus"], inputs["session_uid"]
    revision = first.set_state(domain, uid, "restricted", 1)
    first.close()
    restarted = Catalog(path)
    survived = restarted.session(domain, uid)
    restricted = refusal(lambda: archive.evidence(restarted, store, grant(inputs), domain, uid))
    revision = restarted.set_state(domain, uid, "retained", revision)
    exact = ranges_match(inputs, restarted, store)
    replayed = capture(inputs, restarted, store, root / "worker")
    with pytest.raises(Conflict):
        restarted.set_state(domain, uid, "deleted", 1)
    found = archive.evidence(restarted, store, grant(inputs), domain, uid)
    assert exact and found["revision"] == revision == 3
    assert restricted["error"] == "EvidenceRefused"
    legacy = [restarted.events(domain, uid, item["source_generation"]) for item in found["sources"]]
    retained = {table: len(rows) for table, rows in restarted.snapshot().items() if table in ROLLBACK_TABLES}
    assert legacy == [item["events"] for item in found["sources"]] and all(retained.values())
    return {
        "survived_restart": survived,
        "restricted_evidence": restricted,
        "rollback_rehearsal": {"legacy_event_reads_match_evidence": True, "retained_rows": retained},
        "source_ranges_exact_after_restart": exact,
        "replayed": [item["state"] for item in replayed],
        "stale_revision_refused": True,
        "evidence": shape(found),
        "canonical_copies": [len(item["events"]) for item in found["sources"]],
        "metrics": metrics(restarted, inputs),
    }


CASES = {"a": positive, "b": rejection, "c": recovery}


def run_case(case):
    inputs = json.loads(FIXTURE.read_text(encoding="utf-8"))
    with tempfile.TemporaryDirectory() as folder:
        observed = CASES[case](inputs, Path(folder))
    return {
        "case": f"T-SV2-IDX-01-{case.upper()}",
        "state": "passed",
        "evidence_class": EVIDENCE_CLASS,
        "input_sha256": {FIXTURE.name: hashlib.sha256(FIXTURE.read_bytes()).hexdigest()},
        "observed": observed,
    }
