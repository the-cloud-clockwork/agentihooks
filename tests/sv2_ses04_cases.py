import hashlib
import json
import tempfile
from pathlib import Path

import pytest

from hooks.memory.api.ingest import SCHEMA_VERSION, Grant, ingest
from hooks.memory.catalog import Catalog

FIXTURE = Path(__file__).parent / "fixtures/swarm_v2/ingest-batches.json"
EVIDENCE_CLASS = (
    "mocked: an in-process caller and a SQLite catalog file stand in for the collector, the network and the "
    "central database; no HTTP server, Pod or hive collector runs"
)


class ResponseLost(BaseException):
    pass


class CrashAfterCommit(Catalog):
    def commit(self, batch):
        super().commit(batch)
        raise ResponseLost


def grant(inputs):
    data = inputs["grant"]
    return Grant(data["principal"], data["corpus"], frozenset(data["projects"]))


def request(inputs, name):
    return {
        "schema_version": SCHEMA_VERSION,
        "batch_id": inputs[name]["batch_id"],
        "access_domain": inputs["grant"]["corpus"],
        "session_uid": inputs["session_uid"],
        "source_generation": inputs["source_generation"],
        "events": inputs[name]["events"],
    }


def stored(catalog, inputs):
    return catalog.events(inputs["grant"]["corpus"], inputs["session_uid"], inputs["source_generation"])


def counters(catalog, inputs):
    return {
        "access_domain": inputs["grant"]["corpus"],
        "ingest_duplicate_events_total": catalog.duplicate_events_total(inputs["grant"]["corpus"]),
    }


def summary(response):
    return {key: response[key] for key in sorted(response) if key not in ("schema_version", "session_uid")}


def positive(inputs, root):
    first = Catalog(root / "catalog.sqlite3")
    committed = ingest(first, grant(inputs), request(inputs, "valid"))
    first.close()
    replacement = Catalog(root / "catalog.sqlite3")
    retried = ingest(replacement, grant(inputs), request(inputs, "valid"))
    overlap = ingest(replacement, grant(inputs), request(inputs, "duplicate"))
    events = stored(replacement, inputs)
    assert committed["state"] == "committed" and retried["state"] == "replayed"
    assert retried["accepted"] == committed["accepted"]
    assert [event["sequence"] for event in events] == [1, 2, 3, 4]
    assert len({event["event_id"] for event in events}) == 4
    submitted = {
        event["sequence"]: event["record"] for event in inputs["valid"]["events"] + inputs["duplicate"]["events"]
    }
    assert [event["record"] for event in events] == [submitted[sequence] for sequence in (1, 2, 3, 4)]
    assert overlap["duplicates"] == committed["accepted"]["event_ids"][1:]
    assert [item["sequence"] for item in overlap["rejected"]] == [5]
    assert overlap["durable_through"] == 5
    return {
        "committed": summary(committed),
        "retried_from_replacement_connection": summary(retried),
        "overlap": summary(overlap),
        "stored_sequences": [event["sequence"] for event in events],
        "stored_event_ids": [event["event_id"] for event in events],
        "canonical_copies": len(events),
        "quarantined": replacement.quarantined(
            inputs["grant"]["corpus"], inputs["session_uid"], inputs["source_generation"]
        ),
        "metrics": counters(replacement, inputs),
    }


def rejection(inputs, root):
    catalog = Catalog(root / "catalog.sqlite3")
    ingest(catalog, grant(inputs), request(inputs, "valid"))
    protected = catalog.snapshot()
    mixed = ingest(catalog, grant(inputs), request(inputs, "mixed_scope"))
    again = ingest(catalog, grant(inputs), request(inputs, "mixed_scope"))
    foreign = {**request(inputs, "next"), "access_domain": "personal-corpus"}
    other_corpus = ingest(catalog, grant(inputs), foreign)
    rewritten = ingest(catalog, grant(inputs), request(inputs, "rewritten"))
    reused = {**request(inputs, "rewritten"), "batch_id": inputs["valid"]["batch_id"]}
    batch_conflict = ingest(catalog, grant(inputs), reused)
    assert catalog.snapshot() == protected
    diagnostics = json.dumps([mixed, again, other_corpus, rewritten, batch_conflict], ensure_ascii=False)
    for hidden in ("personal-notes", "private personal prompt", "personal-corpus", "rewritten reply two"):
        assert hidden not in diagnostics
    assert mixed == again
    corrected = ingest(catalog, grant(inputs), request(inputs, "next"))
    assert corrected["state"] == "committed" and corrected["durable_through"] == 4
    return {
        "mixed_scope": mixed,
        "mixed_scope_retried": again,
        "other_corpus": other_corpus,
        "rewritten_event": rewritten,
        "reused_batch_id": batch_conflict,
        "protected_state_unchanged": True,
        "corrected_request": summary(corrected),
        "stored_sequences": [event["sequence"] for event in stored(catalog, inputs)],
        "metrics": counters(catalog, inputs),
    }


def recovery(inputs, root):
    crashing = CrashAfterCommit(root / "catalog.sqlite3")
    with pytest.raises(ResponseLost):
        ingest(crashing, grant(inputs), request(inputs, "valid"))
    crashing.close()
    restarted = Catalog(root / "catalog.sqlite3")
    survived = stored(restarted, inputs)
    retried = ingest(restarted, grant(inputs), request(inputs, "valid"))
    assert retried["state"] == "replayed" and retried["durable_through"] == 3
    assert stored(restarted, inputs) == survived
    advanced = ingest(restarted, grant(inputs), request(inputs, "next"))
    stale = ingest(restarted, grant(inputs), request(inputs, "valid"))
    assert advanced["durable_through"] == 4 and stale["durable_through"] == 4
    events = stored(restarted, inputs)
    assert [event["sequence"] for event in events] == [1, 2, 3, 4]
    return {
        "survived_crash": [event["sequence"] for event in survived],
        "retried_after_restart": summary(retried),
        "advanced": summary(advanced),
        "stale_retry": summary(stale),
        "stored_sequences": [event["sequence"] for event in events],
        "canonical_copies": len(events),
        "watermark": restarted.watermark(inputs["grant"]["corpus"], inputs["session_uid"], inputs["source_generation"]),
        "metrics": counters(restarted, inputs),
    }


CASES = {"a": positive, "b": rejection, "c": recovery}


def run_case(case):
    inputs = json.loads(FIXTURE.read_text(encoding="utf-8"))
    with tempfile.TemporaryDirectory() as folder:
        observed = CASES[case](inputs, Path(folder))
    return {
        "case": f"T-SV2-SES-04-{case.upper()}",
        "state": "passed",
        "evidence_class": EVIDENCE_CLASS,
        "input_sha256": {FIXTURE.name: hashlib.sha256(FIXTURE.read_bytes()).hexdigest()},
        "observed": observed,
    }
