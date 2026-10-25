"""SV2-IDX-03: bounded incremental embedding worker."""

import json
from pathlib import Path

import pytest

from hooks.memory import embeddings, index_worker
from hooks.memory.archive import EvidenceRefused
from hooks.memory.catalog import Catalog, Conflict
from hooks.memory.embeddings import EmbeddingModel
from hooks.memory.index_worker import BACKLOG, IndexWorker, Limits, Target
from tests import sv2_idx03_cases
from tests.sv2_idx02_cases import grant
from tests.sv2_idx03_cases import FixtureEmbedder, Index

pytestmark = pytest.mark.unit

EVIDENCE = Path(__file__).parent.parent / "evidence" / "SV2-IDX-03"
MODEL = EmbeddingModel("fixture-hash-embedder-4", 4)
DOMAIN, GENERATION = "swarm-fixture", "fixture-embed-v1"


@pytest.fixture
def inputs():
    return json.loads(sv2_idx03_cases.FIXTURE.read_text(encoding="utf-8"))


def indexed(inputs, tmp_path, limits=None, turns=None):
    if limits:
        inputs["limits"] = limits
    index = Index(inputs, tmp_path)
    if turns != []:
        index.session.append(inputs["turns"][:-1] if turns is None else turns)
    index.session.build()
    index.worker.register(DOMAIN, GENERATION, MODEL)
    return index


def chunk_ids(index):
    return [chunk["chunk_id"] for chunk in index.session.chunks("current")]


def work(index):
    return index.worker.snapshot()["embedding_work"]


def test_a_model_needs_an_identity_and_a_positive_dimension():
    assert embeddings.require(EmbeddingModel("m", 1)) == EmbeddingModel("m", 1)
    for bad in (EmbeddingModel("", 4), EmbeddingModel("m", 0)):
        with pytest.raises(ValueError, match=f"^{embeddings.BAD_MODEL}$"):
            embeddings.require(bad)


def test_vectors_must_match_the_model_count_and_dimension():
    assert embeddings.checked(EmbeddingModel("m", 2), ["a"], [[1, 2]]) == [[1.0, 2.0]]
    assert type(embeddings.checked(EmbeddingModel("m", 1), ["a"], [[1]])[0][0]) is float
    for texts, vectors in ((["a"], [[1.0, 2.0], [3.0, 4.0]]), (["a"], [[1.0]]), (["a", "b"], [[1.0, 2.0], [3.0]])):
        with pytest.raises(Conflict, match=f"^{embeddings.DIMENSION_MISMATCH}$"):
            embeddings.checked(EmbeddingModel("m", 2), texts, vectors)


def test_limits_must_all_be_positive(tmp_path):
    catalog = Catalog(tmp_path / "c.sqlite3")
    assert index_worker.DEFAULT_LIMITS == Limits(32, 64000, 8)
    assert IndexWorker(catalog, Limits(1, 1, 1)).limits == Limits(1, 1, 1)
    assert IndexWorker(catalog).limits == index_worker.DEFAULT_LIMITS
    for bad in (Limits(0, 1, 1), Limits(1, 0, 1), Limits(1, 1, 0)):
        with pytest.raises(ValueError, match=f"^{index_worker.BAD_LIMITS}$"):
            IndexWorker(catalog, bad)


def test_a_target_keys_its_rows_by_domain_generation_session_and_version():
    assert Target("d", "s", "v", "g").key() == ("d", "g", "s", "v")


def test_a_generation_binds_one_model_and_dimension(inputs, tmp_path):
    index = indexed(inputs, tmp_path)
    worker = index.worker
    expected = {"index_generation": GENERATION, "model": MODEL.model, "dimension": 4}
    assert worker.register(DOMAIN, GENERATION, MODEL) == expected
    assert worker.snapshot()["index_generation"] == [(DOMAIN, GENERATION, MODEL.model, 4, 0)]
    for other in (EmbeddingModel(MODEL.model, 5), EmbeddingModel("other", 4)):
        with pytest.raises(Conflict, match=f"^{index_worker.GENERATION_CONFLICT}$"):
            worker.register(DOMAIN, GENERATION, other)
    with pytest.raises(ValueError, match=f"^{embeddings.BAD_MODEL}$"):
        worker.register(DOMAIN, "g2", EmbeddingModel("", 0))
    assert worker.snapshot()["index_generation"] == [(DOMAIN, GENERATION, MODEL.model, 4, 0)]


def test_an_unknown_generation_is_refused(inputs, tmp_path):
    index = indexed(inputs, tmp_path)
    for call in (
        lambda: index.worker.set_paused(DOMAIN, "missing", True),
        lambda: index.worker.drain(FixtureEmbedder(MODEL), DOMAIN, "missing", 0.0),
        lambda: index.worker.activate(DOMAIN, "missing", 0),
        lambda: index.worker.enqueue(grant(inputs), Target(DOMAIN, inputs["session_uid"], "turn-v1", "missing"), 0.0),
    ):
        with pytest.raises(Conflict, match=f"^{index_worker.UNKNOWN_INDEX}$"):
            call()
    assert work(index) == []


def test_pausing_sets_and_clears_the_flag(inputs, tmp_path):
    index = indexed(inputs, tmp_path)
    index.worker.set_paused(DOMAIN, GENERATION, True)
    assert index.worker.snapshot()["index_generation"][0][4] == 1
    index.worker.set_paused(DOMAIN, GENERATION, False)
    assert index.worker.snapshot()["index_generation"][0][4] == 0


def test_enqueue_queues_every_chunk_without_a_vector_once(inputs, tmp_path):
    index = indexed(inputs, tmp_path)
    ids = chunk_ids(index)
    hashes = {chunk["chunk_id"]: chunk["content_hash"] for chunk in index.session.chunks("current")}
    first = index.enqueue(10.0)
    assert first == {"queued": ids, "embedded": 0, "dropped": []}
    rows = [(DOMAIN, GENERATION, cid, inputs["session_uid"], "turn-v1", hashes[cid], 10.0, 0, None) for cid in ids]
    assert work(index) == sorted(rows)
    assert index.enqueue(20.0) == {"queued": [], "embedded": 0, "dropped": []}
    assert work(index) == sorted(rows)


def test_enqueue_counts_embedded_chunks_and_skips_them(inputs, tmp_path):
    index = indexed(inputs, tmp_path)
    index.enqueue(10.0)
    index.drain(FixtureEmbedder(MODEL), 11.0)
    assert index.enqueue(12.0) == {"queued": [], "embedded": len(chunk_ids(index)), "dropped": []}


def test_enqueue_requeues_a_changed_hash_and_resets_its_attempts(inputs, tmp_path):
    index = indexed(inputs, tmp_path)
    index.enqueue(10.0)
    index.drain(FixtureEmbedder(MODEL), 11.0)
    ids = chunk_ids(index)
    db = index.session.catalog.db
    db.execute("UPDATE chunk_vector SET content_hash = 'old' WHERE chunk_id = ?", (ids[0],))
    assert index.enqueue(12.0)["queued"] == [ids[0]]
    db.execute("UPDATE embedding_work SET content_hash = 'old', attempts = 3, last_error = 'E'")
    assert index.enqueue(13.0)["queued"] == [ids[0]]
    assert [row[5:] for row in work(index)] == [(index.session.chunks("current")[0]["content_hash"], 13.0, 0, None)]


def test_enqueue_drops_work_and_vectors_of_superseded_chunks(inputs, tmp_path):
    index = indexed(inputs, tmp_path)
    index.enqueue(10.0)
    index.drain(FixtureEmbedder(MODEL), 11.0)
    before = chunk_ids(index)
    index.session.append([[{"kind": "assistant_text", "text": "a later note in the open turn"}]])
    grown = index.session.build()
    db = index.session.catalog.db
    db.execute(
        "INSERT INTO embedding_work VALUES (?, ?, 'gone', ?, 'turn-v1', 'h', 1.0, 0, NULL)",
        (DOMAIN, GENERATION, inputs["session_uid"]),
    )
    result = index.enqueue(12.0)
    assert result["dropped"] == sorted([*grown["superseded"], "gone"])
    assert result["queued"] == grown["inserted"] and result["embedded"] == len(before) - 1
    assert [row[2] for row in work(index)] == grown["inserted"]
    assert grown["superseded"][0] not in [item["chunk_id"] for item in index.vectors()]
    assert len(index.vectors()) == len(before) - 1


def test_enqueue_is_refused_outside_the_grant(inputs, tmp_path):
    index = indexed(inputs, tmp_path)
    foreign = grant(inputs, "personal-corpus")
    with pytest.raises(EvidenceRefused):
        index.worker.enqueue(foreign, Target("personal-corpus", inputs["session_uid"], "turn-v1", GENERATION), 1.0)
    with pytest.raises(EvidenceRefused):
        index.worker.vectors(foreign, Target("personal-corpus", inputs["session_uid"], "turn-v1", GENERATION))
    with pytest.raises(EvidenceRefused):
        index.worker.coverage(foreign, Target("personal-corpus", inputs["session_uid"], "turn-v1", GENERATION))
    assert work(index) == []


def test_drain_embeds_in_order_and_commits_vectors_with_model_and_generation(inputs, tmp_path):
    index = indexed(inputs, tmp_path)
    index.enqueue(10.0)
    embedder = FixtureEmbedder(MODEL)
    report = index.drain(embedder, 15.0)
    chunks = index.session.chunks("current")
    ordered = sorted(chunks, key=lambda chunk: chunk["chunk_id"])
    assert report == {
        "state": "drained",
        "embedded": [c["chunk_id"] for c in ordered],
        "failed": [],
        "pending": 0,
        BACKLOG: 0.0,
    }
    assert embedder.texts == [chunk["preview"] for chunk in ordered]
    assert embedder.calls == 3
    rows = index.worker.snapshot()["chunk_vector"]
    expected = [
        (DOMAIN, GENERATION, c["chunk_id"], inputs["session_uid"], "turn-v1", c["content_hash"], MODEL.model, 4)
        for c in ordered
    ]
    assert [row[:8] for row in rows] == expected
    assert [json.loads(row[8]) for row in rows] == [sv2_idx03_cases.vector(c["preview"], 4) for c in ordered]
    assert work(index) == []


def test_drain_reports_backlog_age_from_the_oldest_pending_chunk(inputs, tmp_path):
    index = indexed(inputs, tmp_path, {"batch_size": 1, "batch_chars": 4000, "max_batches": 1})
    assert index.worker.pending(DOMAIN, GENERATION, 50.0) == {"pending": 0, BACKLOG: 0.0}
    index.enqueue(10.0)
    total = len(chunk_ids(index))
    report = index.drain(FixtureEmbedder(MODEL), 25.0)
    assert report["state"] == "bounded" and len(report["embedded"]) == 1
    assert {key: report[key] for key in ("pending", BACKLOG)} == {"pending": total - 1, BACKLOG: 15.0}
    assert index.worker.pending(DOMAIN, GENERATION, 5.0) == {"pending": total - 1, BACKLOG: 0.0}


def test_a_batch_is_bounded_by_size_and_characters(inputs, tmp_path):
    index = indexed(inputs, tmp_path)
    index.enqueue(10.0)
    ordered = sorted(index.session.chunks("current"), key=lambda chunk: chunk["chunk_id"])
    two = len(ordered[0]["preview"]) + len(ordered[1]["preview"])
    index.worker.limits = Limits(2, two, 1)
    assert [c["chunk_id"] for c in index.worker._batch(DOMAIN, GENERATION)] == [c["chunk_id"] for c in ordered[:2]]
    index.worker.limits = Limits(3, two - 1, 1)
    assert [c["chunk_id"] for c in index.worker._batch(DOMAIN, GENERATION)] == [ordered[0]["chunk_id"]]
    index.worker.limits = Limits(3, 1, 1)
    assert [c["chunk_id"] for c in index.worker._batch(DOMAIN, GENERATION)] == [ordered[0]["chunk_id"]]
    three = two + len(ordered[2]["preview"])
    index.worker.limits = Limits(3, three - 1, 1)
    assert [c["chunk_id"] for c in index.worker._batch(DOMAIN, GENERATION)] == [c["chunk_id"] for c in ordered[:2]]


def test_a_batch_drops_work_whose_chunk_is_gone_or_changed(inputs, tmp_path):
    index = indexed(inputs, tmp_path)
    index.enqueue(10.0)
    ordered = sorted(chunk_ids(index))
    db = index.session.catalog.db
    db.execute("UPDATE embedding_work SET content_hash = 'changed' WHERE chunk_id = ?", (ordered[0],))
    db.execute("UPDATE embedding_work SET chunk_id = 'gone' WHERE chunk_id = ?", (ordered[1],))
    batch = index.worker._batch(DOMAIN, GENERATION)
    assert [chunk["chunk_id"] for chunk in batch] == ordered[2:4]
    assert [row[2] for row in work(index)] == ordered[2:]
    assert ordered[0] not in [row[2] for row in work(index)] and "gone" not in [row[2] for row in work(index)]


def test_a_commit_skips_work_already_taken(inputs, tmp_path):
    index = indexed(inputs, tmp_path)
    index.enqueue(10.0)
    batch = index.worker._batch(DOMAIN, GENERATION)
    taken = index.worker._commit((DOMAIN, GENERATION), MODEL, batch, [[0.0] * 4] * len(batch))
    assert taken == [chunk["chunk_id"] for chunk in batch]
    assert index.worker._commit((DOMAIN, GENERATION), MODEL, batch, [[1.0] * 4] * len(batch)) == []
    assert [json.loads(row[8]) for row in index.worker.snapshot()["chunk_vector"]] == [[0.0] * 4] * len(batch)


def test_a_commit_writes_the_rest_of_a_batch_after_a_taken_chunk(inputs, tmp_path):
    index = indexed(inputs, tmp_path)
    index.enqueue(10.0)
    batch = index.worker._batch(DOMAIN, GENERATION)
    index.session.catalog.db.execute("DELETE FROM embedding_work WHERE chunk_id = ?", (batch[0]["chunk_id"],))
    written = index.worker._commit((DOMAIN, GENERATION), MODEL, batch, [[0.0] * 4] * len(batch))
    assert written == [chunk["chunk_id"] for chunk in batch[1:]] and len(batch) == 2


def test_a_transient_failure_keeps_earlier_batches_and_stops(inputs, tmp_path):
    index = indexed(inputs, tmp_path)
    index.enqueue(10.0)
    ordered = sorted(chunk_ids(index))
    embedder = FixtureEmbedder(MODEL, fail_calls={2})
    report = index.drain(embedder, 30.0)
    assert report == {
        "state": "deferred",
        "embedded": ordered[:2],
        "failed": ordered[2:4],
        "pending": len(ordered) - 2,
        BACKLOG: 20.0,
    }
    assert embedder.calls == 2
    rows = {row[2]: row[7:] for row in work(index)}
    assert rows == {cid: ((1, "TransientEmbeddingError") if cid in ordered[2:4] else (0, None)) for cid in ordered[2:]}
    assert index.coverage() == {"state": "incomplete", "chunks": len(ordered), "embedded": 2}
    index.drain(FixtureEmbedder(MODEL), 31.0)
    assert index.coverage() == {"state": "indexed", "chunks": len(ordered), "embedded": len(ordered)}


def test_a_mismatched_model_or_dimension_writes_nothing(inputs, tmp_path):
    index = indexed(inputs, tmp_path)
    index.enqueue(10.0)
    before = index.worker.snapshot()
    other = FixtureEmbedder(EmbeddingModel("other", 4))
    with pytest.raises(Conflict, match=f"^{index_worker.MODEL_MISMATCH}$"):
        index.drain(other, 11.0)
    narrow = FixtureEmbedder(MODEL, width=3)
    with pytest.raises(Conflict, match=f"^{embeddings.DIMENSION_MISMATCH}$"):
        index.drain(narrow, 11.0)
    assert other.calls == 0 and narrow.calls == 1
    assert index.worker.snapshot() == before


def test_a_paused_generation_embeds_nothing(inputs, tmp_path):
    index = indexed(inputs, tmp_path)
    index.enqueue(10.0)
    index.worker.set_paused(DOMAIN, GENERATION, True)
    embedder = FixtureEmbedder(MODEL)
    total = len(chunk_ids(index))
    report = index.drain(embedder, 12.0)
    assert report == {"state": "paused", "embedded": [], "failed": [], "pending": total, BACKLOG: 2.0}
    assert embedder.calls == 0
    index.worker.set_paused(DOMAIN, GENERATION, False)
    assert index.drain(embedder, 13.0)["state"] == "drained"


def test_vectors_and_coverage_read_the_session_under_the_grant(inputs, tmp_path):
    index = indexed(inputs, tmp_path)
    assert index.coverage() == {"state": "incomplete", "chunks": len(chunk_ids(index)), "embedded": 0}
    assert index.vectors() == []
    index.enqueue(10.0)
    index.drain(FixtureEmbedder(MODEL), 11.0)
    first = index.session.chunks("current")[0]
    found = {item["chunk_id"]: item for item in index.vectors()}[first["chunk_id"]]
    assert found == {
        "chunk_id": first["chunk_id"],
        "content_hash": first["content_hash"],
        "model": MODEL.model,
        "dimension": 4,
        "vector": sv2_idx03_cases.vector(first["preview"], 4),
    }
    assert [item["chunk_id"] for item in index.vectors()] == sorted(chunk_ids(index))
    index.session.catalog.db.execute(
        "UPDATE chunk_vector SET content_hash = 'old' WHERE chunk_id = ?", (first["chunk_id"],)
    )
    assert index.coverage()["embedded"] == len(chunk_ids(index)) - 1


def test_an_empty_session_counts_as_indexed(inputs, tmp_path):
    index = indexed(inputs, tmp_path, turns=[])
    assert index.coverage() == {"state": "indexed", "chunks": 0, "embedded": 0}


def test_activation_needs_a_complete_generation_and_the_current_revision(inputs, tmp_path):
    index = indexed(inputs, tmp_path)
    worker = index.worker
    assert worker.active(DOMAIN) == {"index_generation": None, "revision": 0}
    index.enqueue(10.0)
    with pytest.raises(Conflict, match=f"^{index_worker.INCOMPLETE}$"):
        worker.activate(DOMAIN, GENERATION, 0)
    index.drain(FixtureEmbedder(MODEL), 11.0)
    with pytest.raises(Conflict, match=f"^{index_worker.STALE_CUTOVER}$"):
        worker.activate(DOMAIN, GENERATION, 1)
    assert worker.activate(DOMAIN, GENERATION, 0) == 1
    assert worker.active(DOMAIN) == {"index_generation": GENERATION, "revision": 1}
    worker.register(DOMAIN, "g2", EmbeddingModel("m8", 8))
    assert worker.activate(DOMAIN, "g2", 1) == 2
    assert worker.active(DOMAIN) == {"index_generation": "g2", "revision": 2}
    assert worker.snapshot()["index_cutover"] == [(DOMAIN, "g2", 2)]


def test_activation_needs_every_chunk_of_a_tracked_session_embedded(inputs, tmp_path):
    index = indexed(inputs, tmp_path)
    index.enqueue(10.0)
    index.drain(FixtureEmbedder(MODEL), 11.0)
    index.session.append(inputs["turns"][-1:])
    index.session.build()
    with pytest.raises(Conflict, match=f"^{index_worker.INCOMPLETE}$"):
        index.worker.activate(DOMAIN, GENERATION, 0)
    index.enqueue(12.0)
    index.drain(FixtureEmbedder(MODEL), 13.0)
    first = chunk_ids(index)[0]
    db = index.session.catalog.db
    db.execute("UPDATE chunk_vector SET content_hash = 'old' WHERE chunk_id = ?", (first,))
    with pytest.raises(Conflict, match=f"^{index_worker.INCOMPLETE}$"):
        index.worker.activate(DOMAIN, GENERATION, 0)
    db.execute(
        "UPDATE chunk_vector SET content_hash = ? WHERE chunk_id = ?",
        (index.session.chunks("current")[0]["content_hash"], first),
    )
    worker = index.worker
    worker.register(DOMAIN, "g2", EmbeddingModel("m8", 8))
    assert worker.activate(DOMAIN, "g2", 0) == 1
    assert worker.activate(DOMAIN, GENERATION, 1) == 2


def test_activation_needs_every_retained_session_with_an_active_chunk_generation(inputs, tmp_path):
    index = indexed(inputs, tmp_path)
    worker = index.worker
    index.session.validate()
    index.session.activate("current", 0)
    with pytest.raises(Conflict, match=f"^{index_worker.INCOMPLETE}$"):
        worker.activate(DOMAIN, GENERATION, 0)
    revision = index.session.catalog.session(DOMAIN, inputs["session_uid"])["revision"]
    index.session.catalog.set_state(DOMAIN, inputs["session_uid"], "deleted", revision)
    assert worker.activate(DOMAIN, GENERATION, 0) == 1
    index.session.catalog.set_state(DOMAIN, inputs["session_uid"], "retained", revision + 1)
    worker.register(DOMAIN, "g2", MODEL)
    with pytest.raises(Conflict, match=f"^{index_worker.INCOMPLETE}$"):
        worker.activate(DOMAIN, "g2", 1)
    index.enqueue(10.0, "index_generation")
    index.drain(FixtureEmbedder(MODEL), 11.0)
    assert worker.activate(DOMAIN, GENERATION, 1) == 2


def test_the_snapshot_covers_every_index_table(inputs, tmp_path):
    assert set(indexed(inputs, tmp_path).worker.snapshot()) == set(index_worker.INDEX_TABLES)


@pytest.mark.parametrize("case", ["a", "b", "c"])
def test_package_cases_match_their_committed_evidence(case):
    first, second = sv2_idx03_cases.run_case(case), sv2_idx03_cases.run_case(case)
    assert first == second
    assert first["state"] == "passed", json.dumps(first, indent=2, sort_keys=True, ensure_ascii=False)
    path = EVIDENCE / f"{case}-result.json"
    committed = json.loads(path.read_text(encoding="utf-8")) if path.exists() else None
    assert committed == first, json.dumps(first, indent=2, sort_keys=True, ensure_ascii=False)


def test_the_package_result_names_the_case_records_and_states():
    manifest = json.loads((EVIDENCE / "manifest.json").read_text(encoding="utf-8"))
    result = json.loads((EVIDENCE / "result.json").read_text(encoding="utf-8"))
    for name, value in sv2_idx03_cases.run_case("a")["input_sha256"].items():
        assert manifest["inputs"][f"tests/fixtures/swarm_v2/{name}"] == value
    assert manifest["cases"] == ["T-SV2-IDX-03-A", "T-SV2-IDX-03-B", "T-SV2-IDX-03-C"]
    assert result["package"] == "SV2-IDX-03"
    assert result["package_complete"] is False
    assert set(result["states"]) == {"observed", "accepted", "committed", "externally_verified"}
    assert set(result["metrics"]) == {BACKLOG}
    for case in "abc":
        assert (EVIDENCE.parents[1] / result["cases"][case]).exists()
