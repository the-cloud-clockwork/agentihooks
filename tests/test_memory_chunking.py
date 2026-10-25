"""SV2-IDX-02: stable turn and tool evidence chunking."""

import hashlib
import json
from pathlib import Path

import pytest
from jsonschema import ValidationError

from hooks.memory import archive, chunking
from hooks.memory import catalog as catalog_module
from hooks.memory.api.ingest import SCHEMA_VERSION, Grant, ingest
from hooks.memory.catalog import Catalog, Conflict
from tests import sv2_idx02_cases

pytestmark = pytest.mark.unit

EVIDENCE = Path(__file__).parent.parent / "evidence" / "SV2-IDX-02"
GRANT = Grant("reader", "corpus-a", frozenset({"project-a"}))
SCOPE = chunking.ChunkScope("corpus-a", "s1", "parent-1")
POLICY = chunking.Policy("v1", 40)


def event(sequence, kind, project="project-a", **fields):
    return {
        "event_id": f"e{sequence}",
        "sequence": sequence,
        "byte_start": sequence * 10,
        "byte_end": sequence * 10 + 10,
        "project_id": project,
        "record": {"kind": kind, **fields},
    }


def turn_events():
    return [
        event(1, "user_text", text="list"),
        event(2, "assistant_text", text="ok"),
        event(3, "tool_call", None, tool_name="Bash", tool_input={"command": "ls"}, tool_use_id="a"),
        event(4, "tool_call", None, tool_name="Read", tool_input="x.py", tool_use_id="b"),
        event(5, "tool_result", tool_result="one two", tool_use_id="a", is_error=False),
        event(6, "tool_result", tool_result=[{"text": "x"}], tool_use_id="b", is_error=True),
        event(7, "token_usage"),
        event(8, "assistant_text", text="done"),
        event(9, "user_text", text="next"),
    ]


def sha(text):
    return hashlib.sha256(text.encode()).hexdigest()


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def chunk_id(sequences, kind, version="v1", generation="g1"):
    return "chunk-" + digest([version, "corpus-a", "s1", generation, kind, sequences])[:40]


def turn_id(first, version="v1", generation="g1"):
    return "turn-" + digest([version, "corpus-a", "s1", generation, first])[:40]


def evidence_of(*sequences):
    return [{"event_id": f"e{n}", "sequence": n, "byte_start": n * 10, "byte_end": n * 10 + 10} for n in sequences]


def test_lines_label_each_record_kind():
    assert chunking._line({"kind": "user_text", "text": "a"}) == "user: a"
    assert chunking._line({"kind": "assistant_text"}) == "assistant: "
    assert chunking._line({"kind": "system_text", "text": "s"}) == "system: s"
    assert chunking._line({"kind": "turn_complete", "text": "t"}) == "summary: t"
    assert chunking._line({"kind": "tool_call", "tool_name": "Bash", "tool_input": {"b": 1, "a": "é"}}) == (
        'tool Bash: {"a": "é", "b": 1}'
    )
    assert chunking._line({"kind": "tool_call"}) == "tool : null"
    assert chunking._line({"kind": "tool_result", "tool_result": "out"}) == "result: out"
    assert chunking._line({"kind": "tool_result", "tool_result": ["x"], "is_error": True}) == 'result error: ["x"]'


def test_turns_start_at_each_user_text_and_keep_a_leading_partial_turn():
    events = [event(1, "assistant_text"), event(2, "user_text"), event(3, "tool_call"), event(4, "user_text")]
    assert [[item["sequence"] for item in turn] for turn in chunking._turns(events)] == [[1], [2, 3], [4]]
    assert chunking._turns([]) == []


def test_parts_pack_whole_events_up_to_the_bound_counting_separators():
    events = [event(n, "user_text", text="x") for n in (1, 2, 3)]
    assert [[item["sequence"] for item in part] for part in chunking._parts(events, 15)] == [[1, 2], [3]]
    assert [[item["sequence"] for item in part] for part in chunking._parts(events, 14)] == [[1], [2], [3]]
    assert [[item["sequence"] for item in part] for part in chunking._parts(events, 23)] == [[1, 2, 3]]
    assert [[item["sequence"] for item in part] for part in chunking._parts(events, 2)] == [[1], [2], [3]]


def test_tools_pair_calls_and_results_by_use_id_and_keep_unlinked_events_apart():
    events = [
        event(1, "tool_call", tool_use_id="a"),
        event(2, "user_text"),
        event(3, "tool_call", tool_use_id=""),
        event(4, "tool_result", tool_use_id="a"),
        event(5, "tool_result"),
    ]
    assert [[item["sequence"] for item in pair] for pair in chunking._tools(events)] == [[1, 4], [3], [5]]


def test_tool_links_call_and_result_or_leaves_the_missing_side_empty():
    call = event(1, "tool_call", tool_name="Bash", tool_use_id="a")
    result = event(2, "tool_result", tool_use_id="a", is_error=1)
    assert chunking._tool([call, result]) == {
        "tool_use_id": "a",
        "tool_name": "Bash",
        "is_error": True,
        "call_event_id": "e1",
        "result_event_id": "e2",
    }
    assert chunking._tool([event(1, "tool_call")]) == {
        "tool_use_id": "",
        "tool_name": "",
        "is_error": None,
        "call_event_id": "e1",
        "result_event_id": None,
    }
    assert chunking._tool([event(2, "tool_result", tool_use_id="z")]) == {
        "tool_use_id": "z",
        "tool_name": None,
        "is_error": False,
        "call_event_id": None,
        "result_event_id": "e2",
    }


def test_a_multi_tool_turn_yields_turn_parts_and_linked_tool_chunks():
    chunks = chunking.chunk_events(chunking.Policy("v1", 2000), SCOPE, "g1", list(reversed(turn_events())))
    first = turn_id(1)
    assert [(chunk["kind"], [item["sequence"] for item in chunk["evidence"]]) for chunk in chunks] == [
        ("turn", [1, 2, 8]),
        ("tool", [3, 5]),
        ("tool", [4, 6]),
        ("turn", [9]),
    ]
    narrative = "user: list\nassistant: ok\nassistant: done"
    assert chunks[0] == {
        "chunk_id": chunk_id([1, 2, 8], "turn"),
        "chunker_version": "v1",
        "access_domain": "corpus-a",
        "session_uid": "s1",
        "parent_session_uid": "parent-1",
        "source_generation": "g1",
        "turn_id": first,
        "kind": "turn",
        "project_ids": ["project-a"],
        "preview": narrative,
        "truncated": False,
        "omitted_chars": 0,
        "content_hash": sha(narrative),
        "tool": None,
        "evidence": evidence_of(1, 2, 8),
    }
    assert chunks[1]["preview"] == 'tool Bash: {"command": "ls"}\nresult: one two'
    assert chunks[1]["tool"]["call_event_id"] == "e3" and chunks[1]["tool"]["result_event_id"] == "e5"
    assert chunks[2]["tool"]["is_error"] is True and chunks[2]["chunk_id"] == chunk_id([4, 6], "tool")
    assert chunks[1]["project_ids"] == ["project-a"] and chunks[1]["turn_id"] == chunks[2]["turn_id"] == first
    assert chunks[3]["turn_id"] == turn_id(9)


def test_long_tool_output_is_bounded_for_embedding_with_its_full_locator():
    long = "y" * 100
    chunks = chunking.chunk_events(POLICY, SCOPE, "g1", [event(1, "tool_result", tool_result=long, tool_use_id="a")])
    text = f"result: {long}"
    assert chunks[0]["preview"] == text[:40]
    assert chunks[0]["truncated"] is True and chunks[0]["omitted_chars"] == len(text) - 40
    assert chunks[0]["content_hash"] == sha(text[:40])
    assert chunking.locator(chunks[0]) == [
        {"access_domain": "corpus-a", "session_uid": "s1", "source_generation": "g1", "byte_start": 10, "byte_end": 20}
    ]
    exact = chunking.chunk_events(
        chunking.Policy("v1", len(text)), SCOPE, "g1", [event(1, "tool_result", tool_result=long)]
    )
    assert exact[0]["truncated"] is False and exact[0]["omitted_chars"] == 0


def test_appending_events_keeps_every_earlier_chunk_identical():
    policy = chunking.Policy("v1", 2000)
    earlier = chunking.chunk_events(policy, SCOPE, "g1", turn_events()[:8])
    grown = chunking.chunk_events(policy, SCOPE, "g1", turn_events() + [event(10, "assistant_text", text="z")])
    assert grown[: len(earlier)] == earlier
    assert grown[-1]["evidence"] == evidence_of(9, 10)


def test_ids_change_with_version_generation_and_scope():
    events = turn_events()[:2]
    base = chunking.chunk_events(POLICY, SCOPE, "g1", events)[0]["chunk_id"]
    assert base == chunk_id([1, 2], "turn")
    assert chunking.chunk_events(chunking.Policy("v2", 40), SCOPE, "g1", events)[0]["chunk_id"] != base
    assert chunking.chunk_events(POLICY, SCOPE, "g2", events)[0]["chunk_id"] != base
    other = chunking.ChunkScope("corpus-b", "s1")
    assert chunking.chunk_events(POLICY, other, "g1", events)[0]["chunk_id"] != base
    assert chunking.chunk_events(POLICY, chunking.ChunkScope("corpus-a", "s2"), "g1", events)[0]["chunk_id"] != base


@pytest.mark.parametrize("policy", [chunking.Policy("", 10), chunking.Policy("v1", 0)])
def test_a_policy_without_a_version_or_bound_is_refused(policy):
    with pytest.raises(ValueError, match=chunking.BAD_POLICY):
        chunking.chunk_events(policy, SCOPE, "g1", [])


def test_chunk_records_match_the_schema():
    validator = sv2_idx02_cases.schema_validator()
    scope = chunking.ChunkScope("corpus-a", "s1", None)
    events = [{**item, "project_id": "local:a" if item["project_id"] else None} for item in turn_events()]
    for chunk in chunking.chunk_events(POLICY, scope, "g1", events):
        validator.validate(chunk)
    with pytest.raises(ValidationError):
        validator.validate({**chunk, "extra": 1})


@pytest.fixture
def catalog(tmp_path):
    opened = Catalog(tmp_path / "catalog.sqlite3")
    yield opened
    opened.close()


@pytest.fixture
def store(tmp_path):
    return archive.ObjectStore(tmp_path / "objects")


def line(record):
    return json.dumps(record).encode() + b"\n"


class Writer:
    def __init__(self, catalog, store):
        self.catalog, self.store, self.sequence, self.cursor, self.batch = catalog, store, 0, 0, 0
        source = catalog_module.SessionSource(
            "corpus-a", "s1", "g1", "claude", "worker:s1.jsonl", "retain-until-deleted"
        )
        catalog.register(source)

    def append(self, *records):
        events, data, start = [], b"", self.cursor
        for record in records:
            self.sequence += 1
            encoded = line(record)
            events.append(
                {
                    "sequence": self.sequence,
                    "byte_start": self.cursor,
                    "byte_end": self.cursor + len(encoded),
                    "project_id": "project-a",
                    "record": record,
                }
            )
            data, self.cursor = data + encoded, self.cursor + len(encoded)
        self.catalog.archive(
            catalog_module.ArchiveEntry("corpus-a", "s1", "g1", start, self.cursor, self.store.put(data))
        )
        self.batch += 1
        body = {
            "schema_version": SCHEMA_VERSION,
            "batch_id": f"b{self.batch}",
            "access_domain": "corpus-a",
            "session_uid": "s1",
            "source_generation": "g1",
            "events": events,
        }
        assert ingest(self.catalog, GRANT, body)["state"] == "committed"


USER = {"kind": "user_text", "text": "hello"}
REPLY = {"kind": "assistant_text", "text": "hi"}
CALL = {"kind": "tool_call", "tool_name": "Bash", "tool_input": {"command": "ls"}, "tool_use_id": "a"}
RESULT = {"kind": "tool_result", "tool_result": "x", "tool_use_id": "a", "is_error": False}


@pytest.fixture
def writer(catalog, store):
    return Writer(catalog, store)


@pytest.fixture
def index(catalog):
    return chunking.ChunkIndex(catalog)


def test_the_index_adds_its_tables_to_the_catalog_database(catalog, index):
    assert set(index.snapshot()) == set(chunking.CHUNK_TABLES)
    assert all(rows == [] for rows in index.snapshot().values())
    chunking.ChunkIndex(catalog)
    assert index.cutover("corpus-a", "s1") == {"chunker_version": None, "revision": 0}
    assert index.rebuilt_total("corpus-a") == 0


def test_build_stores_a_generation_and_reports_what_changed(writer, index):
    writer.append(USER, CALL, RESULT, REPLY)
    built = index.build(GRANT, SCOPE, POLICY)
    stored = index.chunks(GRANT, "corpus-a", "s1", "v1")
    assert built == {
        "state": "committed",
        "generation_state": "built",
        "inserted": [chunk["chunk_id"] for chunk in stored],
        "superseded": [],
        "unchanged": 0,
        chunking.REBUILT: 0,
        "chunker_version": "v1",
        "built_through": {"g1": 4},
    }
    assert [chunk["kind"] for chunk in stored] == ["turn", "tool"]
    assert stored == chunking.chunk_events(POLICY, SCOPE, "g1", writer.catalog.events("corpus-a", "s1", "g1"))
    generation = index.snapshot()["chunk_generation"]
    assert generation == [
        ("corpus-a", "s1", "v1", json.dumps({"version": "v1", "max_chars": 40}), "built", '{"g1": 4}')
    ]


def test_a_repeated_build_is_a_replay_with_no_effect(writer, index):
    writer.append(USER, REPLY)
    index.build(GRANT, SCOPE, POLICY)
    index.validate(writer.store, GRANT, "corpus-a", "s1", "v1")
    before = index.snapshot()
    again = index.build(GRANT, SCOPE, POLICY)
    assert again["inserted"] == [] and again["superseded"] == [] and again["unchanged"] == 1
    assert again["generation_state"] == "validated"
    assert index.snapshot() == before


def test_appending_a_turn_inserts_only_new_chunks_and_resets_validation(writer, index):
    writer.append(USER, CALL, RESULT, REPLY)
    first = index.build(GRANT, SCOPE, POLICY)
    index.validate(writer.store, GRANT, "corpus-a", "s1", "v1")
    rows = {row[3]: row for row in index.snapshot()["search_chunk"]}
    writer.append(USER, REPLY)
    grown = index.build(GRANT, SCOPE, POLICY)
    assert grown["unchanged"] == len(first["inserted"]) and len(grown["inserted"]) == 1
    assert grown["generation_state"] == "built" and grown["built_through"] == {"g1": 6}
    after = {row[3]: row for row in index.snapshot()["search_chunk"]}
    assert all(after[chunk_id] == row for chunk_id, row in rows.items())


def test_a_growing_open_turn_supersedes_only_its_own_last_chunk(writer, index):
    writer.append(USER)
    first = index.build(GRANT, SCOPE, POLICY)
    writer.append(REPLY)
    grown = index.build(GRANT, SCOPE, POLICY)
    assert grown["superseded"] == first["inserted"] and len(grown["inserted"]) == 1
    assert grown[chunking.REBUILT] == 0
    assert [row[3] for row in index.snapshot()["search_chunk"]] == grown["inserted"]


def test_a_renumbered_chunk_with_the_same_content_is_counted(writer, index, catalog):
    writer.append(USER, REPLY)
    index.build(GRANT, SCOPE, POLICY)
    catalog.db.execute("UPDATE search_chunk SET chunk_id = 'chunk-old'")
    rebuilt = index.build(GRANT, SCOPE, POLICY)
    assert rebuilt["superseded"] == ["chunk-old"] and rebuilt[chunking.REBUILT] == 1
    assert index.rebuilt_total("corpus-a") == 1
    catalog.db.execute(
        "UPDATE search_chunk SET chunk_id = 'chunk-old', record = json_set(record, '$.content_hash', 'z')"
    )
    assert index.build(GRANT, SCOPE, POLICY)[chunking.REBUILT] == 0
    assert index.rebuilt_total("corpus-a") == 1


def test_a_policy_change_under_one_version_is_refused(writer, index):
    writer.append(USER)
    index.build(GRANT, SCOPE, POLICY)
    before = index.snapshot()
    with pytest.raises(Conflict, match=chunking.POLICY_CONFLICT):
        index.build(GRANT, SCOPE, chunking.Policy("v1", 41))
    assert index.snapshot() == before


def test_a_changed_parent_cannot_rewrite_a_stored_chunk(writer, index):
    writer.append(USER)
    index.build(GRANT, SCOPE, POLICY)
    before = index.snapshot()
    with pytest.raises(Conflict, match=chunking.CHUNK_CONFLICT):
        index.build(GRANT, chunking.ChunkScope("corpus-a", "s1", "other"), POLICY)
    assert index.snapshot() == before


def test_a_build_that_read_fewer_events_cannot_overwrite_a_newer_one(writer, index, monkeypatch):
    writer.append(USER)
    stale = archive._readable(writer.catalog, GRANT, "corpus-a", "s1")
    writer.append(REPLY)
    index.build(GRANT, SCOPE, POLICY)
    before = index.snapshot()
    with monkeypatch.context() as patch:
        patch.setattr(archive, "_readable", lambda *_: stale)
        with pytest.raises(Conflict, match=chunking.STALE_BUILD):
            index.build(GRANT, SCOPE, POLICY)
        missing = (stale[0], [])
        patch.setattr(archive, "_readable", lambda *_: missing)
        with pytest.raises(Conflict, match=chunking.STALE_BUILD):
            index.build(GRANT, SCOPE, POLICY)
    assert index.snapshot() == before


def test_a_source_with_no_events_builds_an_empty_generation(index, writer):
    built = index.build(GRANT, SCOPE, POLICY)
    assert built["built_through"] == {"g1": 0} and built["inserted"] == []


def test_reads_and_builds_obey_the_archive_grant(writer, index):
    writer.append(USER)
    index.build(GRANT, SCOPE, POLICY)
    foreign = Grant("reader", "corpus-b", frozenset({"project-a"}))
    before = index.snapshot()
    with pytest.raises(archive.EvidenceRefused):
        index.build(foreign, chunking.ChunkScope("corpus-b", "s1"), POLICY)
    with pytest.raises(archive.EvidenceRefused):
        index.chunks(foreign, "corpus-a", "s1", "v1")
    with pytest.raises(archive.EvidenceRefused):
        index.active(foreign, "corpus-a", "s1")
    assert index.snapshot() == before


def test_unknown_generations_are_refused(writer, index):
    writer.append(USER)
    with pytest.raises(Conflict, match=chunking.UNKNOWN_GENERATION):
        index.chunks(GRANT, "corpus-a", "s1", "v1")
    with pytest.raises(Conflict, match=chunking.NOT_VALIDATED):
        index.activate("corpus-a", "s1", "v1", 0)


def test_validation_reads_the_archive_before_cutover(writer, index, store):
    writer.append(USER, CALL, RESULT)
    index.build(GRANT, SCOPE, POLICY)
    with pytest.raises(Conflict, match=chunking.NOT_VALIDATED):
        index.activate("corpus-a", "s1", "v1", 0)
    digest = writer.catalog.manifest("corpus-a", "s1", "g1")[0]["object_digest"]
    path = store.root / digest[:2] / digest
    kept = path.read_bytes()
    path.unlink()
    before = index.snapshot()
    with pytest.raises(archive.EvidenceIntegrityError, match=archive.MISSING):
        index.validate(store, GRANT, "corpus-a", "s1", "v1")
    assert index.snapshot() == before
    store.put(kept)
    assert index.validate(store, GRANT, "corpus-a", "s1", "v1") == {
        "chunker_version": "v1",
        "state": "validated",
        "chunks": 2,
    }
    assert index.snapshot()["chunk_generation"][0][4] == "validated"


@pytest.mark.parametrize("field", ["event_id", "sequence", "byte_start", "byte_end"])
def test_a_chunk_naming_a_range_the_catalog_lacks_fails_validation(writer, index, catalog, field):
    writer.append(USER)
    index.build(GRANT, SCOPE, POLICY)
    value = '"e-other"' if field == "event_id" else "999"
    catalog.db.execute(f"UPDATE search_chunk SET record = json_set(record, '$.evidence[0].{field}', json('{value}'))")
    with pytest.raises(archive.EvidenceIntegrityError, match=chunking.UNSUPPORTED):
        index.validate(writer.store, GRANT, "corpus-a", "s1", "v1")
    assert index.snapshot()["chunk_generation"][0][4] == "built"


def test_a_chunk_from_another_source_generation_fails_validation(writer, index, catalog):
    writer.append(USER)
    index.build(GRANT, SCOPE, POLICY)
    catalog.db.execute("UPDATE search_chunk SET record = json_set(record, '$.source_generation', 'g9')")
    with pytest.raises(archive.EvidenceIntegrityError, match=chunking.UNSUPPORTED):
        index.validate(writer.store, GRANT, "corpus-a", "s1", "v1")


def test_cutover_is_compare_and_set_and_rollback_restores_the_prior_generation(writer, index, store):
    writer.append(USER, REPLY)
    index.build(GRANT, SCOPE, POLICY)
    index.build(GRANT, SCOPE, chunking.Policy("v2", 5))
    for version in ("v1", "v2"):
        index.validate(store, GRANT, "corpus-a", "s1", version)
    assert index.active(GRANT, "corpus-a", "s1") == {"chunker_version": None, "revision": 0, "chunks": []}
    assert index.activate("corpus-a", "s1", "v1", 0) == 1
    old = index.active(GRANT, "corpus-a", "s1")
    assert old == {"chunker_version": "v1", "revision": 1, "chunks": index.chunks(GRANT, "corpus-a", "s1", "v1")}
    assert index.activate("corpus-a", "s1", "v2", 1) == 2
    assert index.active(GRANT, "corpus-a", "s1")["chunks"] == index.chunks(GRANT, "corpus-a", "s1", "v2")
    before = index.snapshot()
    with pytest.raises(Conflict, match=chunking.STALE_CUTOVER):
        index.activate("corpus-a", "s1", "v1", 1)
    assert index.snapshot() == before
    assert index.activate("corpus-a", "s1", "v1", 2) == 3
    assert index.active(GRANT, "corpus-a", "s1") == {**old, "revision": 3}
    assert index.cutover("corpus-a", "s1") == {"chunker_version": "v1", "revision": 3}


def test_the_cutover_survives_a_restart(tmp_path, store):
    first = Catalog(tmp_path / "c.sqlite3")
    Writer(first, store).append(USER)
    chunking.ChunkIndex(first).build(GRANT, SCOPE, POLICY)
    chunking.ChunkIndex(first).validate(store, GRANT, "corpus-a", "s1", "v1")
    chunking.ChunkIndex(first).activate("corpus-a", "s1", "v1", 0)
    first.close()
    reopened = Catalog(tmp_path / "c.sqlite3")
    assert chunking.ChunkIndex(reopened).cutover("corpus-a", "s1") == {"chunker_version": "v1", "revision": 1}
    reopened.close()


@pytest.mark.parametrize("case", ["a", "b", "c"])
def test_package_cases_match_their_committed_evidence(case):
    first, second = sv2_idx02_cases.run_case(case), sv2_idx02_cases.run_case(case)
    assert first == second
    assert first["state"] == "passed", json.dumps(first, indent=2, sort_keys=True, ensure_ascii=False)
    path = EVIDENCE / f"{case}-result.json"
    committed = json.loads(path.read_text(encoding="utf-8")) if path.exists() else None
    assert committed == first, json.dumps(first, indent=2, sort_keys=True, ensure_ascii=False)


def test_the_package_result_names_the_case_records_and_states():
    manifest = json.loads((EVIDENCE / "manifest.json").read_text(encoding="utf-8"))
    result = json.loads((EVIDENCE / "result.json").read_text(encoding="utf-8"))
    for name, value in sv2_idx02_cases.run_case("a")["input_sha256"].items():
        assert manifest["inputs"][f"tests/fixtures/swarm_v2/{name}"] == value
    assert manifest["cases"] == ["T-SV2-IDX-02-A", "T-SV2-IDX-02-B", "T-SV2-IDX-02-C"]
    assert result["package"] == "SV2-IDX-02"
    assert result["package_complete"] is False
    assert set(result["states"]) == {"observed", "accepted", "committed", "externally_verified"}
    assert set(result["metrics"]) == {chunking.REBUILT}
    for case in "abc":
        assert (EVIDENCE.parents[1] / result["cases"][case]).exists()
