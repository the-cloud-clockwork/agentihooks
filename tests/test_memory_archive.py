"""SV2-IDX-01: durable session and record storage."""

import hashlib
import json
from pathlib import Path

import pytest

from hooks.memory import archive
from hooks.memory import catalog as catalog_module
from hooks.memory.api.ingest import SCHEMA_VERSION, Grant, ingest
from hooks.memory.catalog import Catalog, Conflict
from tests import sv2_idx01_cases

pytestmark = pytest.mark.unit

EVIDENCE = Path(__file__).parent.parent / "evidence" / "SV2-IDX-01"
GRANT = Grant("reader", "corpus-a", frozenset({"project-a"}))
LINES = [b'{"kind":"user_text"}\n', b'{"kind":"assistant_text"}\n', b'{"kind":"tool_call"}\n']
SESSION = {"provider": "claude", "retention": "retain-until-deleted", "state": "retained", "revision": 1}


def source(generation="gen-1", origin="worker-a:session.jsonl", **overrides):
    values = {
        "access_domain": "corpus-a",
        "session_uid": "s1",
        "source_generation": generation,
        "provider": "claude",
        "origin": origin,
        "retention": "retain-until-deleted",
        **overrides,
    }
    return catalog_module.SessionSource(**values)


def entry(start, end, digest, generation="gen-1"):
    return catalog_module.ArchiveEntry("corpus-a", "s1", generation, start, end, digest)


def reference(start, end, generation="gen-1", domain="corpus-a"):
    return {
        "access_domain": domain,
        "session_uid": "s1",
        "source_generation": generation,
        "byte_start": start,
        "byte_end": end,
    }


def sha(data):
    return hashlib.sha256(data).hexdigest()


@pytest.fixture
def catalog(tmp_path):
    opened = Catalog(tmp_path / "catalog.sqlite3")
    yield opened
    opened.close()


@pytest.fixture
def store(tmp_path):
    return archive.ObjectStore(tmp_path / "objects")


def events(projects=("project-a", "project-a", None), kinds=("user_text", "assistant_text", "tool_call")):
    built, cursor = [], 0
    for sequence, (project, kind, line) in enumerate(zip(projects, kinds, LINES), start=1):
        built.append(
            {
                "sequence": sequence,
                "byte_start": cursor,
                "byte_end": cursor + len(line),
                "project_id": project,
                "record": {"kind": kind, "text": f"text {sequence}"},
            }
        )
        cursor += len(line)
    return built


def captured(catalog, store, generation="gen-1", split=1, **kwargs):
    catalog.register(source(generation))
    data = b"".join(LINES)
    cut = sum(len(line) for line in LINES[:split])
    for start, end in ((0, cut), (cut, len(data))):
        catalog.archive(entry(start, end, store.put(data[start:end]), generation))
    grant = Grant("collector", "corpus-a", frozenset({"project-a", "project-b"}))
    body = {
        "schema_version": SCHEMA_VERSION,
        "batch_id": f"batch-{generation}",
        "access_domain": "corpus-a",
        "session_uid": "s1",
        "source_generation": generation,
        "events": events(**kwargs),
    }
    assert ingest(catalog, grant, body)["state"] == "committed"
    return data, cut


def object_path(store, digest):
    return store.root / digest[:2] / digest


def refused(error_type, call):
    with pytest.raises(error_type) as raised:
        call()
    return str(raised.value)


def test_new_tables_are_part_of_the_catalog_and_its_snapshot(catalog):
    assert catalog_module.TABLES[-3:] == ("session_record", "session_source", "archive_manifest")
    assert set(catalog.snapshot()) == set(catalog_module.TABLES)


def test_a_registered_session_starts_retained_at_revision_one_with_its_source(catalog):
    assert catalog.register(source()) == SESSION
    assert catalog.session("corpus-a", "s1") == SESSION
    assert catalog.sources("corpus-a", "s1") == [{"source_generation": "gen-1", "origin": "worker-a:session.jsonl"}]


def test_registering_again_keeps_one_row_and_a_new_generation_adds_a_source(catalog):
    catalog.register(source())
    assert catalog.register(source()) == SESSION
    catalog.register(source("gen-0", "worker-b:session.jsonl"))
    assert catalog.sources("corpus-a", "s1") == [
        {"source_generation": "gen-0", "origin": "worker-b:session.jsonl"},
        {"source_generation": "gen-1", "origin": "worker-a:session.jsonl"},
    ]
    assert len(catalog.snapshot()["session_record"]) == 1


@pytest.mark.parametrize(
    "changed, message",
    [
        ({"provider": "codex"}, catalog_module.SESSION_CONFLICT),
        ({"retention": "expire-after-30-days"}, catalog_module.SESSION_CONFLICT),
        ({"origin": "worker-c:other.jsonl"}, catalog_module.SOURCE_CONFLICT),
    ],
)
def test_a_registration_that_changes_provider_retention_or_origin_conflicts_and_changes_nothing(
    catalog, changed, message
):
    catalog.register(source())
    before = catalog.snapshot()
    assert refused(Conflict, lambda: catalog.register(source(**changed))) == message
    assert catalog.snapshot() == before
    assert (
        message
        == {
            catalog_module.SESSION_CONFLICT: "the session was registered with another provider or retention",
            catalog_module.SOURCE_CONFLICT: "the source generation was registered with another origin",
        }[message]
    )


def test_unknown_sessions_have_no_record_sources_or_manifest(catalog):
    assert catalog.session("corpus-a", "s1") is None
    assert catalog.sources("corpus-a", "s1") == []
    assert catalog.manifest("corpus-a", "s1", "gen-1") == []


def test_a_state_change_at_the_current_revision_advances_the_revision(catalog):
    catalog.register(source())
    assert catalog.set_state("corpus-a", "s1", "deleted", 1) == 2
    assert catalog.session("corpus-a", "s1") == {**SESSION, "state": "deleted", "revision": 2}
    assert catalog.set_state("corpus-a", "s1", "restricted", 2) == 3
    assert catalog.session("corpus-a", "s1")["state"] == "restricted"


@pytest.mark.parametrize("domain, revision", [("corpus-a", 2), ("corpus-a", 0), ("corpus-b", 1)])
def test_a_stale_revision_or_unknown_session_conflicts_and_changes_nothing(catalog, domain, revision):
    catalog.register(source())
    before = catalog.snapshot()
    message = refused(Conflict, lambda: catalog.set_state(domain, "s1", "deleted", revision))
    assert message == "the session is unknown or its revision is stale; nothing changed"
    assert catalog.snapshot() == before


def test_an_unknown_state_is_refused_and_changes_nothing(catalog):
    catalog.register(source())
    before = catalog.snapshot()
    message = refused(ValueError, lambda: catalog.set_state("corpus-a", "s1", "gone", 1))
    assert message == "the session state must be retained, restricted or deleted"
    assert catalog_module.SESSION_STATES == ("retained", "restricted", "deleted")
    assert catalog.snapshot() == before


def test_a_state_change_is_scoped_to_its_access_domain(catalog):
    catalog.register(source())
    catalog.register(source(access_domain="corpus-b"))
    catalog.set_state("corpus-b", "s1", "deleted", 1)
    assert catalog.session("corpus-a", "s1") == SESSION


def test_an_archive_entry_is_recorded_once_and_listed_by_offset(catalog):
    catalog.register(source())
    assert catalog.archive(entry(50, 90, "b" * 64)) is True
    assert catalog.archive(entry(0, 50, "a" * 64)) is True
    assert catalog.archive(entry(0, 50, "a" * 64)) is False
    assert catalog.manifest("corpus-a", "s1", "gen-1") == [
        {"byte_start": 0, "byte_end": 50, "object_digest": "a" * 64},
        {"byte_start": 50, "byte_end": 90, "object_digest": "b" * 64},
    ]
    assert catalog.manifest("corpus-a", "s1", "gen-2") == []


@pytest.mark.parametrize("changed", [entry(0, 50, "c" * 64), entry(0, 40, "a" * 64)])
def test_an_archive_entry_rewritten_at_its_offset_conflicts_and_changes_nothing(catalog, changed):
    catalog.register(source())
    catalog.archive(entry(0, 50, "a" * 64))
    before = catalog.snapshot()
    message = refused(Conflict, lambda: catalog.archive(changed))
    assert message == "an archive object at this offset was recorded with different content"
    assert catalog.snapshot() == before


def test_an_archive_entry_for_an_unregistered_generation_conflicts(catalog):
    catalog.register(source())
    message = refused(Conflict, lambda: catalog.archive(entry(0, 50, "a" * 64, "gen-9")))
    assert message == "the source generation is not registered"
    assert catalog.snapshot()["archive_manifest"] == []


def test_integrity_failures_are_counted_per_domain_beside_duplicates(catalog):
    assert catalog.integrity_failures_total("corpus-a") == 0
    catalog.integrity_failure("corpus-a")
    catalog.integrity_failure("corpus-a")
    assert catalog.integrity_failures_total("corpus-a") == 2
    assert catalog.integrity_failures_total("corpus-b") == 0
    assert catalog.duplicate_events_total("corpus-a") == 0
    assert catalog_module.ARCHIVE_FAILURES == "archive_integrity_failures_total"


def test_an_object_is_stored_once_under_its_digest(store, monkeypatch):
    synced = []
    monkeypatch.setattr(archive.os, "fsync", lambda descriptor: synced.append(descriptor))
    digest = store.put(b"evidence")
    assert digest == sha(b"evidence")
    assert object_path(store, digest).read_bytes() == b"evidence"
    assert store.put(b"evidence") == digest
    assert [path.name for path in object_path(store, digest).parent.iterdir()] == [digest]
    assert len(synced) == 4
    assert store.get(digest) == b"evidence"


def test_an_object_is_written_through_a_temporary_file_beside_it(store, monkeypatch):
    real, directories = archive.tempfile.mkstemp, []

    def recording(dir):
        directories.append(Path(dir))
        return real(dir=dir)

    monkeypatch.setattr(archive.tempfile, "mkstemp", recording)
    digest = store.put(b"evidence")
    assert directories == [object_path(store, digest).parent]


def test_a_failed_write_leaves_no_temporary_file_and_no_object(store, monkeypatch):
    def failing(descriptor):
        raise OSError("disk full")

    monkeypatch.setattr(archive.os, "fsync", failing)
    assert refused(OSError, lambda: store.put(b"evidence")) == "disk full"
    assert list(object_path(store, sha(b"evidence")).parent.iterdir()) == []


def test_a_missing_object_is_an_integrity_error(store):
    message = refused(archive.EvidenceIntegrityError, lambda: store.get(sha(b"absent")))
    assert message == "an archived object is missing; the evidence is incomplete and was not returned"


def test_an_altered_object_is_an_integrity_error(store):
    digest = store.put(b"evidence")
    object_path(store, digest).write_bytes(b"altered")
    message = refused(archive.EvidenceIntegrityError, lambda: store.get(digest))
    assert message == archive.ALTERED
    assert archive.ALTERED == (
        "an archived object does not match its digest or range; the evidence is incomplete and was not returned"
    )


@pytest.mark.parametrize("name", ["../" + "a" * 61, "A" * 64, "a" * 63, "a" * 65])
def test_an_object_name_must_be_a_sha256_digest(store, name):
    assert refused(ValueError, lambda: store.get(name)) == "an object name must be a sha256 hex digest"


def test_evidence_returns_every_event_quarantine_and_verified_object(catalog, store):
    data, cut = captured(catalog, store, kinds=("user_text", "provider_noise", "tool_call"))
    found = archive.evidence(catalog, store, GRANT, "corpus-a", "s1")
    stored = catalog.events("corpus-a", "s1", "gen-1")
    assert found == {
        "complete": True,
        "access_domain": "corpus-a",
        "session_uid": "s1",
        **SESSION,
        "sources": [
            {
                "source_generation": "gen-1",
                "origin": "worker-a:session.jsonl",
                "events": stored,
                "quarantined": catalog.quarantined("corpus-a", "s1", "gen-1"),
                "archive": [
                    {"byte_start": 0, "byte_end": cut, "object_digest": sha(data[:cut]), "byte_size": cut},
                    {
                        "byte_start": cut,
                        "byte_end": len(data),
                        "object_digest": sha(data[cut:]),
                        "byte_size": len(data) - cut,
                    },
                ],
            }
        ],
    }
    assert [event["sequence"] for event in stored] == [1, 3]
    assert [item["sequence"] for item in found["sources"][0]["quarantined"]] == [2]


def test_a_missing_object_refuses_the_whole_session_and_is_counted(catalog, store):
    data, cut = captured(catalog, store)
    captured(catalog, store, "gen-2")
    object_path(store, sha(data[cut:])).unlink()
    before = {table: rows for table, rows in catalog.snapshot().items() if table != "ingest_counter"}
    message = refused(archive.EvidenceIntegrityError, lambda: archive.evidence(catalog, store, GRANT, "corpus-a", "s1"))
    assert message == archive.MISSING
    assert catalog.integrity_failures_total("corpus-a") == 1
    assert {table: rows for table, rows in catalog.snapshot().items() if table != "ingest_counter"} == before


def test_an_object_whose_size_differs_from_its_range_is_altered(catalog, store):
    catalog.register(source())
    catalog.archive(entry(0, 9, store.put(b"evidence")))
    message = refused(archive.EvidenceIntegrityError, lambda: archive.evidence(catalog, store, GRANT, "corpus-a", "s1"))
    assert message == archive.ALTERED
    assert catalog.integrity_failures_total("corpus-a") == 1


def archived_events(catalog, store, ranges):
    catalog.register(source())
    data = b"".join(LINES)
    for start, end in ranges:
        catalog.archive(entry(start, end, store.put(data[start:end])))
    body = {
        "schema_version": SCHEMA_VERSION,
        "batch_id": "batch-gen-1",
        "access_domain": "corpus-a",
        "session_uid": "s1",
        "source_generation": "gen-1",
        "events": events(),
    }
    assert ingest(catalog, Grant("collector", "corpus-a", frozenset({"project-a"})), body)["state"] == "committed"
    return data


@pytest.mark.parametrize("ranges", [[(0, 21)], [(0, 21), (22, 68)], [(1, 68)]])
def test_events_beyond_the_archived_ranges_refuse_the_session_and_are_counted(catalog, store, ranges):
    archived_events(catalog, store, ranges)
    message = refused(archive.EvidenceIntegrityError, lambda: archive.evidence(catalog, store, GRANT, "corpus-a", "s1"))
    assert message == archive.UNCOVERED
    assert catalog.integrity_failures_total("corpus-a") == 1


def test_an_object_inside_an_earlier_object_still_covers_the_session(catalog, store):
    data = archived_events(catalog, store, [(0, 68), (5, 10)])
    assert len(data) == 68
    found = archive.evidence(catalog, store, GRANT, "corpus-a", "s1")
    assert found["complete"] is True
    assert [item["byte_end"] for item in found["sources"][0]["archive"]] == [68, 10]
    assert catalog.integrity_failures_total("corpus-a") == 0


@pytest.mark.parametrize("state", ["restricted", "deleted"])
def test_a_session_that_is_not_retained_is_refused_uncounted(catalog, store, state):
    captured(catalog, store)
    catalog.set_state("corpus-a", "s1", state, 1)
    for call in (
        lambda: archive.evidence(catalog, store, GRANT, "corpus-a", "s1"),
        lambda: archive.source_range(catalog, store, GRANT, reference(0, 5)),
    ):
        assert refused(archive.EvidenceRefused, call) == "the session is not retained"
    assert catalog.integrity_failures_total("corpus-a") == 0


def test_an_unknown_session_is_refused_as_not_retained(catalog, store):
    message = refused(archive.EvidenceRefused, lambda: archive.evidence(catalog, store, GRANT, "corpus-a", "s1"))
    assert message == archive.UNAVAILABLE


def test_another_corpus_is_forbidden_before_any_read(catalog, store):
    captured(catalog, store)
    for call in (
        lambda: archive.evidence(catalog, store, GRANT, "corpus-b", "s1"),
        lambda: archive.source_range(catalog, store, GRANT, reference(0, 5, domain="corpus-b")),
    ):
        assert refused(archive.EvidenceRefused, call) == "the grant does not cover this session"


def test_an_event_outside_the_project_grant_forbids_the_session_and_its_ranges(catalog, store):
    captured(catalog, store, projects=("project-a", "project-b", None))
    for call in (
        lambda: archive.evidence(catalog, store, GRANT, "corpus-a", "s1"),
        lambda: archive.source_range(catalog, store, GRANT, reference(0, 5)),
    ):
        assert refused(archive.EvidenceRefused, call) == archive.FORBIDDEN
    wider = Grant("reader", "corpus-a", frozenset({"project-a", "project-b"}))
    assert archive.evidence(catalog, store, wider, "corpus-a", "s1")["complete"] is True


def test_a_source_range_reads_exact_bytes_within_and_across_objects(catalog, store):
    data, cut = captured(catalog, store)
    assert archive.source_range(catalog, store, GRANT, reference(0, cut)) == LINES[0]
    assert archive.source_range(catalog, store, GRANT, reference(2, cut + 3)) == data[2 : cut + 3]
    assert archive.source_range(catalog, store, GRANT, reference(cut + 1, len(data))) == data[cut + 1 :]
    assert archive.source_range(catalog, store, GRANT, reference(0, len(data))) == data
    assert archive.source_range(catalog, store, GRANT, reference(4, 4)) == b""


def test_a_range_ending_at_an_object_boundary_does_not_read_the_next_object(catalog, store):
    data, cut = captured(catalog, store)
    object_path(store, sha(data[cut:])).unlink()
    assert archive.source_range(catalog, store, GRANT, reference(1, cut)) == data[1:cut]
    assert catalog.integrity_failures_total("corpus-a") == 0


def test_a_range_starting_at_an_object_boundary_does_not_read_the_previous_object(catalog, store):
    data, cut = captured(catalog, store)
    object_path(store, sha(data[:cut])).unlink()
    assert archive.source_range(catalog, store, GRANT, reference(cut, len(data))) == data[cut:]
    assert catalog.integrity_failures_total("corpus-a") == 0


@pytest.mark.parametrize("start, end", [(0, 999), (1000, 1001)])
def test_a_range_beyond_the_archive_is_an_integrity_error(catalog, store, start, end):
    captured(catalog, store)
    message = refused(
        archive.EvidenceIntegrityError, lambda: archive.source_range(catalog, store, GRANT, reference(start, end))
    )
    assert (
        message
        == "the requested range is not covered by archived objects; the evidence is incomplete and was not returned"
    )
    assert catalog.integrity_failures_total("corpus-a") == 1


def test_a_gap_between_objects_is_an_integrity_error(catalog, store):
    catalog.register(source())
    catalog.archive(entry(0, 3, store.put(b"abc")))
    catalog.archive(entry(5, 8, store.put(b"fgh")))
    message = refused(
        archive.EvidenceIntegrityError, lambda: archive.source_range(catalog, store, GRANT, reference(1, 7))
    )
    assert message == archive.UNCOVERED
    assert archive.source_range(catalog, store, GRANT, reference(5, 7)) == b"fg"


def test_a_missing_object_inside_the_range_is_counted(catalog, store):
    data, cut = captured(catalog, store)
    object_path(store, sha(data[:cut])).unlink()
    message = refused(
        archive.EvidenceIntegrityError, lambda: archive.source_range(catalog, store, GRANT, reference(0, 3))
    )
    assert message == archive.MISSING
    assert catalog.integrity_failures_total("corpus-a") == 1


def test_a_preview_is_bounded_and_never_complete():
    found = {
        "access_domain": "corpus-a",
        "session_uid": "s1",
        "sources": [
            {"events": [{"record": {"kind": "user_text", "text": "abcdef"}}, {"record": {"kind": "tool_call"}}]},
            {"events": [{"record": {"kind": "assistant_text", "text": "ghij"}}]},
        ],
    }
    assert archive.preview(found, 4) == {
        "complete": False,
        "access_domain": "corpus-a",
        "session_uid": "s1",
        "text": "abcd",
        "omitted_chars": 7,
    }
    assert archive.preview(found)["text"] == "abcdef\nghij"
    assert archive.preview(found)["omitted_chars"] == 0
    assert archive.preview(found, 11)["omitted_chars"] == 0
    assert archive.LEGACY_PREVIEW_CHARS == 4000


@pytest.mark.parametrize("case", ["a", "b", "c"])
def test_package_cases_match_their_committed_evidence(case):
    first, second = sv2_idx01_cases.run_case(case), sv2_idx01_cases.run_case(case)
    assert first == second
    assert first["state"] == "passed", json.dumps(first, indent=2, sort_keys=True, ensure_ascii=False)
    path = EVIDENCE / f"{case}-result.json"
    committed = json.loads(path.read_text(encoding="utf-8")) if path.exists() else None
    assert committed == first, json.dumps(first, indent=2, sort_keys=True, ensure_ascii=False)


def test_the_package_result_names_the_case_records_and_states():
    manifest = json.loads((EVIDENCE / "manifest.json").read_text(encoding="utf-8"))
    result = json.loads((EVIDENCE / "result.json").read_text(encoding="utf-8"))
    for name, digest in sv2_idx01_cases.run_case("a")["input_sha256"].items():
        assert manifest["inputs"][f"tests/fixtures/swarm_v2/{name}"] == digest
    assert manifest["cases"] == ["T-SV2-IDX-01-A", "T-SV2-IDX-01-B", "T-SV2-IDX-01-C"]
    assert result["package"] == "SV2-IDX-01"
    assert result["package_complete"] is False
    assert set(result["states"]) == {"observed", "accepted", "committed", "externally_verified"}
    for case in "abc":
        assert (EVIDENCE.parents[1] / result["cases"][case]).exists()
