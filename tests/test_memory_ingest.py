"""SV2-SES-04: idempotent central session ingestion."""

import json
import sqlite3
from pathlib import Path

import pytest

from hooks.memory import catalog as catalog_module
from hooks.memory.api import ingest as ingest_module
from hooks.memory.api.ingest import RECORD_KINDS, SCHEMA_VERSION, Grant, event_identity, ingest
from hooks.memory.catalog import BatchCommit, Catalog, Conflict, StoredEvent
from tests import sv2_ses04_cases

pytestmark = pytest.mark.unit

EVIDENCE = Path(__file__).parent.parent / "evidence" / "SV2-SES-04"
GRANT = Grant("collector", "corpus-a", frozenset({"project-a", "project-b"}))


def event(sequence, start=None, end=None, project="project-a", text=None):
    start = sequence * 10 if start is None else start
    end = start + 10 if end is None else end
    return {
        "sequence": sequence,
        "byte_start": start,
        "byte_end": end,
        "project_id": project,
        "record": {"kind": "user_text", "text": text or f"prompt {sequence}"},
    }


def request(events, batch_id="batch-1", **overrides):
    body = {
        "schema_version": SCHEMA_VERSION,
        "batch_id": batch_id,
        "access_domain": "corpus-a",
        "session_uid": "corpus-a:claude:host:s1",
        "source_generation": "gen-1",
        "events": events,
    }
    return {**body, **overrides}


@pytest.fixture
def catalog(tmp_path):
    opened = Catalog(tmp_path / "catalog.sqlite3")
    yield opened
    opened.close()


def error(response):
    assert response["state"] == "rejected"
    return response["error"]["class"], response["error"]["detail"]


def test_event_identity_is_stable_and_covers_session_generation_sequence_and_range():
    base = event_identity("s", "g", event(1, 0, 10))
    assert base == event_identity("s", "g", {**event(1, 0, 10), "record": {"kind": "meta"}, "project_id": None})
    assert len(base) == 64
    assert base == ingest_module.digest(["s", "g", 1, 0, 10])
    for other in (
        event_identity("t", "g", event(1, 0, 10)),
        event_identity("s", "h", event(1, 0, 10)),
        event_identity("s", "g", event(2, 0, 10)),
        event_identity("s", "g", event(1, 1, 10)),
        event_identity("s", "g", event(1, 0, 11)),
    ):
        assert other != base


def test_canonical_form_sorts_keys_keeps_unicode_and_drops_spaces():
    assert ingest_module.canonical({"b": "日本", "a": [1, 2]}) == '{"a":[1,2],"b":"日本"}'.encode()
    assert ingest_module.digest({"b": 1, "a": 2}) == ingest_module.digest({"a": 2, "b": 1})


@pytest.mark.parametrize(
    ("body", "detail"),
    [
        ([], "request must be an object"),
        (request([event(1)], schema_version="3.0"), "unsupported schema_version"),
        (request([event(1)], schema_version=2), "unsupported schema_version"),
        (request([event(1)], batch_id=""), "batch_id must be a non-empty string"),
        (request([event(1)], access_domain=None), "access_domain must be a non-empty string"),
        (request([event(1)], session_uid=7), "session_uid must be a non-empty string"),
        (request([event(1)], source_generation=""), "source_generation must be a non-empty string"),
        (request([]), "events must be a non-empty list"),
        (request({"sequence": 1}), "events must be a non-empty list"),
        (request(["x"]), "event 0 must be an object"),
        (request([event(1), {**event(2), "sequence": 0}]), "event 1 sequence must be a positive integer"),
        (request([{**event(1), "sequence": True}]), "event 0 sequence must be a positive integer"),
        (request([{**event(1), "sequence": "1"}]), "event 0 sequence must be a positive integer"),
        (request([event(1, 5, 4)]), "event 0 byte range is invalid"),
        (request([event(1, -1, 4)]), "event 0 byte range is invalid"),
        (request([{**event(1), "byte_end": None}]), "event 0 byte range is invalid"),
        (request([{**event(1), "byte_start": False}]), "event 0 byte range is invalid"),
        (request([event(1, project=3)]), "event 0 project_id must be a string or null"),
        (request([event(1), event(1, 50)]), "event sequences repeat inside the batch"),
        (request([event(1)], principal="operator"), "request carries unknown fields"),
        (request([event(1), {**event(2), "scope": "all"}]), "event 1 carries unknown fields"),
    ],
)
def test_malformed_requests_are_invalid_and_change_nothing(catalog, body, detail):
    before = catalog.snapshot()
    assert error(ingest(catalog, GRANT, body)) == ("invalid_request", detail)
    assert catalog.snapshot() == before


@pytest.mark.parametrize("version", ["2", "2.7"])
def test_a_major_two_schema_version_and_an_empty_byte_range_are_accepted_and_persisted(catalog, version):
    response = ingest(catalog, GRANT, request([event(1, 0, 0)], schema_version=version))
    assert response["state"] == "committed"
    rows = catalog.snapshot()
    assert rows["session_ingest_batch"][0][-1] == version
    assert rows["session_watermark"] == [("corpus-a", "corpus-a:claude:host:s1", "gen-1", 1, version)]


def test_an_event_without_a_project_is_stored_unattributed(catalog):
    body = request([{key: value for key, value in event(1).items() if key != "project_id"}])
    assert ingest(catalog, GRANT, body)["state"] == "committed"
    assert catalog.events("corpus-a", "corpus-a:claude:host:s1", "gen-1")[0]["project_id"] is None


def test_a_refusal_carries_an_operation_id_retry_class_and_schema_version(catalog):
    body = request([event(1)], schema_version="1.0")
    response = ingest(catalog, GRANT, body)
    assert response == {
        "schema_version": SCHEMA_VERSION,
        "operation_id": f"ingest:{ingest_module.digest(body)[:16]}",
        "state": "rejected",
        "error": {"class": "invalid_request", "retry": "never", "detail": "unsupported schema_version"},
    }


def test_a_foreign_corpus_is_forbidden_without_naming_it(catalog):
    response = ingest(catalog, GRANT, request([event(1)], access_domain="corpus-secret"))
    assert error(response) == ("forbidden_scope", "the grant does not cover the requested corpus")
    assert "corpus-secret" not in json.dumps(response)
    assert catalog.snapshot()["session_event"] == []


def test_one_event_outside_the_grant_rejects_the_whole_batch_by_index_only(catalog):
    body = request([event(1), event(2, project="secret-project"), event(3), event(4, project="other")])
    before = catalog.snapshot()
    response = ingest(catalog, GRANT, body)
    assert error(response) == (
        "forbidden_scope",
        "events [1, 3] are outside the grant; the batch was not committed",
    )
    assert "secret-project" not in json.dumps(response)
    assert catalog.snapshot() == before


def test_unattributed_and_every_granted_project_are_accepted(catalog):
    response = ingest(catalog, GRANT, request([event(1, project=None), event(2, project="project-b"), event(3)]))
    assert response["state"] == "committed"
    assert [item["project_id"] for item in catalog.events("corpus-a", "corpus-a:claude:host:s1", "gen-1")] == [
        None,
        "project-b",
        "project-a",
    ]


def test_record_kinds_are_the_normalized_transcript_kinds():
    assert RECORD_KINDS == {
        "user_text",
        "assistant_text",
        "system_text",
        "tool_call",
        "tool_result",
        "token_usage",
        "turn_complete",
        "meta",
    }


@pytest.mark.parametrize(
    ("record", "reason"),
    [
        ("text", "record must be an object"),
        (None, "record must be an object"),
        ({"text": "no kind"}, "record kind is not a supported normalized kind"),
        ({"kind": "provider_noise"}, "record kind is not a supported normalized kind"),
    ],
)
def test_an_unsupported_record_is_quarantined_while_the_rest_commits(catalog, record, reason):
    bad = {**event(2), "record": record}
    response = ingest(catalog, GRANT, request([event(1), bad, event(3)]))
    identity = event_identity("corpus-a:claude:host:s1", "gen-1", bad)
    assert response["state"] == "committed"
    assert response["rejected"] == [{"sequence": 2, "event_id": identity, "reason": reason}]
    assert response["accepted"]["first_sequence"] == 1 and response["accepted"]["last_sequence"] == 3
    assert response["durable_through"] == 3
    assert catalog.quarantined("corpus-a", "corpus-a:claude:host:s1", "gen-1") == [
        {"sequence": 2, "event_id": identity, "reason": reason}
    ]
    assert [item["sequence"] for item in catalog.events("corpus-a", "corpus-a:claude:host:s1", "gen-1")] == [1, 3]


def test_every_supported_kind_is_stored_once_exactly_as_submitted(catalog):
    events = [
        {**event(index + 1), "record": {"kind": kind, "n": index}} for index, kind in enumerate(sorted(RECORD_KINDS))
    ]
    response = ingest(catalog, GRANT, request(events))
    stored = catalog.events("corpus-a", "corpus-a:claude:host:s1", "gen-1")
    assert response["rejected"] == []
    assert [item["record"] for item in stored] == [item["record"] for item in events]


def test_a_committed_response_names_the_batch_range_watermark_and_indexing_state(catalog):
    response = ingest(catalog, GRANT, request([event(2), event(1)]))
    ids = [event_identity("corpus-a:claude:host:s1", "gen-1", event(sequence)) for sequence in (1, 2)]
    assert response == {
        "schema_version": SCHEMA_VERSION,
        "batch_id": "batch-1",
        "session_uid": "corpus-a:claude:host:s1",
        "source_generation": "gen-1",
        "accepted": {"first_sequence": 1, "last_sequence": 2, "event_ids": ids},
        "duplicates": [],
        "rejected": [],
        "state": "committed",
        "durable_through": 2,
        "indexed": False,
    }


def test_stored_events_keep_their_identity_range_project_and_hash(catalog):
    ingest(catalog, GRANT, request([event(1, 3, 9, text="日本")]))
    [stored] = catalog.events("corpus-a", "corpus-a:claude:host:s1", "gen-1")
    assert stored == {
        "event_id": event_identity("corpus-a:claude:host:s1", "gen-1", event(1, 3, 9)),
        "sequence": 1,
        "byte_start": 3,
        "byte_end": 9,
        "project_id": "project-a",
        "record": {"kind": "user_text", "text": "日本"},
        "normalized_hash": ingest_module.digest({"kind": "user_text", "text": "日本"}),
    }


def test_a_batch_of_only_unsupported_records_has_an_empty_accepted_range(catalog):
    response = ingest(catalog, GRANT, request([{**event(1), "record": {"kind": "x"}}]))
    assert response["accepted"] == {"first_sequence": None, "last_sequence": None, "event_ids": []}
    assert response["durable_through"] == 1


def test_a_gap_holds_the_watermark_until_the_missing_sequence_arrives(catalog):
    assert ingest(catalog, GRANT, request([event(1), event(3)]))["durable_through"] == 1
    assert catalog.watermark("corpus-a", "corpus-a:claude:host:s1", "gen-1") == 1
    assert ingest(catalog, GRANT, request([event(2)], batch_id="batch-2"))["durable_through"] == 3
    assert catalog.watermark("corpus-a", "corpus-a:claude:host:s1", "gen-1") == 3


def test_a_batch_starting_after_a_gap_leaves_the_watermark_at_zero(catalog):
    assert ingest(catalog, GRANT, request([event(2)]))["durable_through"] == 0
    assert catalog.watermark("corpus-a", "corpus-a:claude:host:s1", "gen-1") == 0


def test_overlapping_batches_count_duplicates_and_store_one_copy(catalog):
    ingest(catalog, GRANT, request([event(1), event(2)]))
    response = ingest(catalog, GRANT, request([event(2), event(3)], batch_id="batch-2"))
    assert response["duplicates"] == [event_identity("corpus-a:claude:host:s1", "gen-1", event(2))]
    assert response["accepted"]["event_ids"] == [
        event_identity("corpus-a:claude:host:s1", "gen-1", event(sequence)) for sequence in (2, 3)
    ]
    assert len(catalog.events("corpus-a", "corpus-a:claude:host:s1", "gen-1")) == 3
    assert catalog.duplicate_events_total("corpus-a") == 1


def test_a_replayed_batch_returns_its_result_with_the_current_watermark(tmp_path):
    first = Catalog(tmp_path / "c.sqlite3")
    committed = ingest(first, GRANT, request([event(1), event(2)]))
    ingest(first, GRANT, request([event(3)], batch_id="batch-2"))
    first.close()
    replacement = Catalog(tmp_path / "c.sqlite3")
    replayed = ingest(replacement, GRANT, request([event(1), event(2)]))
    assert replayed == {**committed, "state": "replayed", "durable_through": 3}
    assert replacement.duplicate_events_total("corpus-a") == 2
    assert len(replacement.events("corpus-a", "corpus-a:claude:host:s1", "gen-1")) == 3
    replacement.close()


def test_a_reused_batch_id_with_other_content_is_a_conflict_that_changes_nothing(catalog):
    ingest(catalog, GRANT, request([event(1)]))
    before = catalog.snapshot()
    response = ingest(catalog, GRANT, request([event(2)]))
    assert error(response) == ("revision_conflict", "this batch_id was committed with different content")
    assert catalog.snapshot() == before


def test_an_event_rewritten_under_a_new_batch_rolls_the_whole_batch_back(catalog):
    ingest(catalog, GRANT, request([event(2)]))
    before = catalog.snapshot()
    response = ingest(catalog, GRANT, request([event(1), event(2, text="changed")], batch_id="batch-2"))
    assert error(response) == ("revision_conflict", "an event at this sequence was committed with different content")
    assert catalog.snapshot() == before


def test_a_sequence_reused_with_another_byte_range_is_a_conflict(catalog):
    ingest(catalog, GRANT, request([event(1, 0, 10)]))
    response = ingest(catalog, GRANT, request([event(1, 0, 12)], batch_id="batch-2"))
    assert error(response)[0] == "revision_conflict"


def test_a_quarantined_sequence_cannot_later_become_an_event(catalog):
    ingest(catalog, GRANT, request([{**event(1), "record": {"kind": "x"}}]))
    response = ingest(catalog, GRANT, request([event(1)], batch_id="batch-2"))
    assert error(response) == ("revision_conflict", "an event at this sequence was committed with different content")


def test_a_committed_sequence_cannot_later_be_quarantined(catalog):
    ingest(catalog, GRANT, request([event(1)]))
    before = catalog.snapshot()
    response = ingest(catalog, GRANT, request([{**event(1), "record": {"kind": "x"}}], batch_id="batch-2"))
    assert error(response) == ("revision_conflict", "an event at this sequence was committed with different content")
    assert catalog.snapshot() == before


def test_a_repeated_quarantine_from_another_batch_is_kept_once(catalog):
    bad = {**event(1), "record": {"kind": "x"}}
    ingest(catalog, GRANT, request([bad]))
    response = ingest(catalog, GRANT, request([bad], batch_id="batch-2"))
    assert response["state"] == "committed"
    assert len(catalog.quarantined("corpus-a", "corpus-a:claude:host:s1", "gen-1")) == 1


@pytest.mark.parametrize(
    ("first", "second"),
    [
        ({**event(1), "record": {"kind": "x"}}, {**event(1, 0, 12), "record": {"kind": "x"}}),
        ({**event(1), "record": {"kind": "x"}}, {**event(1), "record": "x"}),
    ],
)
def test_a_quarantine_reused_with_another_range_or_reason_is_a_conflict(catalog, first, second):
    ingest(catalog, GRANT, request([first]))
    before = catalog.snapshot()
    response = ingest(catalog, GRANT, request([second], batch_id="batch-2"))
    assert error(response) == ("revision_conflict", "an event at this sequence was committed with different content")
    assert catalog.snapshot() == before


def test_an_event_replayed_under_another_project_is_a_conflict(catalog):
    ingest(catalog, GRANT, request([event(1)]))
    before = catalog.snapshot()
    response = ingest(catalog, GRANT, request([event(1, project="project-b")], batch_id="batch-2"))
    assert error(response) == ("revision_conflict", "an event at this sequence was committed with different content")
    assert catalog.snapshot() == before


def test_an_unhashable_record_kind_is_quarantined(catalog):
    response = ingest(catalog, GRANT, request([{**event(1), "record": {"kind": ["user_text"]}}]))
    assert response["rejected"][0]["reason"] == "record kind is not a supported normalized kind"


def test_a_failed_commit_rolls_back_and_leaves_the_connection_usable(tmp_path, monkeypatch):
    opened = Catalog(tmp_path / "c.sqlite3")
    real = opened.db

    class FailingCommit:
        def __getattr__(self, name):
            return getattr(real, name)

        def execute(self, statement, *args):
            if statement == "COMMIT":
                raise sqlite3.OperationalError("disk I/O error")
            return real.execute(statement, *args)

    monkeypatch.setattr(opened, "db", FailingCommit())
    body = request([event(1)])
    assert ingest(opened, GRANT, body) == {
        "schema_version": SCHEMA_VERSION,
        "operation_id": f"ingest:{ingest_module.digest(body)[:16]}",
        "state": "rejected",
        "error": {
            "class": "dependency_unavailable",
            "retry": "same_batch",
            "detail": "the catalog failed before acknowledging; retrying the same batch is safe",
        },
    }
    monkeypatch.setattr(opened, "db", real)
    assert not real.in_transaction
    assert opened.snapshot() == {table: [] for table in catalog_module.TABLES}
    assert ingest(opened, GRANT, request([event(1)]))["state"] == "committed"
    opened.close()


def test_access_domains_and_generations_keep_separate_batches_events_and_counters(tmp_path):
    catalog = Catalog(tmp_path / "c.sqlite3")
    other = Grant("collector", "corpus-b", frozenset({"project-a"}))
    ingest(catalog, GRANT, request([event(1)]))
    response = ingest(catalog, other, request([event(1)], access_domain="corpus-b"))
    generation = ingest(catalog, GRANT, request([event(1)], batch_id="batch-2", source_generation="gen-2"))
    assert response["state"] == "committed" and generation["state"] == "committed"
    assert catalog.duplicate_events_total("corpus-a") == 0 and catalog.duplicate_events_total("corpus-b") == 0
    assert len(catalog.events("corpus-b", "corpus-a:claude:host:s1", "gen-1")) == 1
    assert catalog.watermark("corpus-a", "corpus-a:claude:host:s1", "gen-2") == 1
    assert catalog.watermark("corpus-a", "corpus-a:claude:host:s1", "gen-3") == 0
    catalog.close()


def test_counters_and_lookups_default_to_empty(catalog):
    assert catalog.duplicate_events_total("corpus-a") == 0
    assert catalog.watermark("corpus-a", "s", "g") == 0
    assert catalog.events("corpus-a", "s", "g") == []
    assert catalog.quarantined("corpus-a", "s", "g") == []
    assert catalog.snapshot() == {table: [] for table in catalog_module.TABLES}


def test_the_catalog_creates_its_folder_and_commits_with_full_sync_in_wal_mode(tmp_path):
    opened = Catalog(tmp_path / "deep" / "catalog.sqlite3")
    assert opened.db.execute("PRAGMA journal_mode").fetchone() == ("wal",)
    assert opened.db.execute("PRAGMA synchronous").fetchone() == (2,)
    assert opened.db.execute("PRAGMA busy_timeout").fetchone() == (5000,)
    opened.close()


def test_a_failure_inside_the_transaction_rolls_back_and_releases_the_lock(tmp_path):
    first = Catalog(tmp_path / "c.sqlite3")
    batch = BatchCommit(
        "corpus-a", "b", "d", "s", "g", [StoredEvent("e", 1, 0, 1, None, {"kind": "meta"}, "h")], [], "2.0"
    )
    with pytest.raises(Conflict):
        with first._transaction():
            first._apply(batch)
            raise Conflict("planted")
    second = Catalog(tmp_path / "c.sqlite3")
    assert second.snapshot() == {table: [] for table in catalog_module.TABLES}
    assert second.commit(batch)["state"] == "committed"
    first.close()
    second.close()


@pytest.mark.parametrize("case", ["a", "b", "c"])
def test_package_cases_match_their_committed_evidence(case):
    first, second = sv2_ses04_cases.run_case(case), sv2_ses04_cases.run_case(case)
    assert first == second
    assert first["state"] == "passed", json.dumps(first, indent=2, sort_keys=True, ensure_ascii=False)
    path = EVIDENCE / f"{case}-result.json"
    committed = json.loads(path.read_text(encoding="utf-8")) if path.exists() else None
    assert committed == first, json.dumps(first, indent=2, sort_keys=True, ensure_ascii=False)


def test_the_package_result_names_the_case_records_and_states():
    manifest = json.loads((EVIDENCE / "manifest.json").read_text(encoding="utf-8"))
    result = json.loads((EVIDENCE / "result.json").read_text(encoding="utf-8"))
    for name, digest in sv2_ses04_cases.run_case("a")["input_sha256"].items():
        assert manifest["inputs"][f"tests/fixtures/swarm_v2/{name}"] == digest
    assert manifest["cases"] == ["T-SV2-SES-04-A", "T-SV2-SES-04-B", "T-SV2-SES-04-C"]
    assert result["package"] == "SV2-SES-04"
    assert result["package_complete"] is False
    assert set(result["states"]) == {"observed", "accepted", "committed", "externally_verified"}
    for case in "abc":
        assert (EVIDENCE.parents[1] / result["cases"][case]).exists()
