import json
import os
from pathlib import Path

import pytest

from hooks.memory import metrics, progress, search, spool
from hooks.memory.api.ingest import SCHEMA_VERSION, Grant
from hooks.memory.archive import EvidenceRefused
from hooks.memory.catalog import BatchCommit, Catalog, Conflict, SessionSource, StoredEvent

pytestmark = pytest.mark.unit

GRANT = Grant("fixture-worker", "swarm-fixture", frozenset({"agentihooks"}))


@pytest.fixture
def captured(tmp_path):
    catalog = Catalog(tmp_path / "catalog.sqlite")
    scope = metrics.Scope(GRANT.corpus, "session-fixture", "generation-one")
    catalog.register(SessionSource(*scope.key(), "claude", "fixture", "retain-until-deleted"))
    event = StoredEvent("event-one", 1, 0, 1, "agentihooks", {"kind": "user_text", "text": "private"}, "hash")
    catalog.commit(
        BatchCommit(
            GRANT.corpus, "batch-one", "digest", scope.session_uid, scope.source_generation, [event], [], SCHEMA_VERSION
        )
    )
    yield catalog, metrics.Metrics(catalog), scope
    catalog.close()


def advance(meter, scope, stage, through, now):
    return meter.advance(GRANT, scope, stage, through, now)


def test_stage_failures_and_recovery_converge_independently(captured):
    catalog, meter, scope = captured
    for stage in metrics.STAGES:
        advance(meter, scope, stage, 1, 100.0)
    advance(meter, scope, "local_spool", 4, 110.0)
    stalled = meter.status(GRANT, scope, 150.0, stale_after=30.0)
    assert stalled["coverage"] == "partial"
    assert stalled["alerts"] == ["archive_stale"]
    assert stalled["archive_lag_seconds"] == 40.0
    assert stalled["embedding_lag_seconds"] == 0.0
    assert stalled["extraction_lag_seconds"] == 0.0
    assert stalled["stages"]["archive"]["backlog"] == 3
    advance(meter, scope, "archive", 4, 155.0)
    delayed = meter.status(GRANT, scope, 160.0, stale_after=30.0)
    assert delayed["archive_lag_seconds"] == 0.0
    assert delayed["embedding_lag_seconds"] == 5.0
    assert delayed["extraction_lag_seconds"] == 5.0
    advance(meter, scope, "indexed_chunk", 4, 170.0)
    extracted_later = meter.status(GRANT, scope, 190.0, stale_after=30.0)
    assert extracted_later["alerts"] == ["extraction_stale"]
    assert extracted_later["embedding_lag_seconds"] == 0.0
    assert extracted_later["extraction_lag_seconds"] == 35.0
    assert extracted_later["stages"]["brain_consumed"]["through"] == 1
    advance(meter, scope, "brain_consumed", 4, 200.0)
    recovered = metrics.Metrics(catalog).status(GRANT, scope, 220.0)
    assert recovered["coverage"] == "complete"
    assert recovered["alerts"] == []
    assert [recovered[name] for name in metrics.LAG_METRICS] == [0.0, 0.0, 0.0]
    assert {stage: value["through"] for stage, value in recovered["stages"].items()} == dict.fromkeys(metrics.STAGES, 4)


def test_retry_cannot_reset_oldest_backlog_age_and_restart_keeps_watermarks(captured, tmp_path):
    _, meter, scope = captured
    advance(meter, scope, "local_spool", 2, 100.0)
    assert advance(meter, scope, "local_spool", 2, 180.0) is False
    advance(meter, scope, "local_spool", 3, 190.0)
    reopened = Catalog(tmp_path / "catalog.sqlite")
    try:
        status = metrics.Metrics(reopened).status(GRANT, scope, 200.0)
        assert status["archive_lag_seconds"] == 100.0
        assert status["stages"]["local_spool"]["through"] == 3
        assert status["stages"]["archive"]["through"] is None
        assert status["coverage"] == "partial"
    finally:
        reopened.close()


@pytest.mark.parametrize("stage", ["archive", "indexed_chunk", "brain_consumed"])
def test_a_stage_cannot_acknowledge_beyond_its_own_upstream(captured, stage):
    catalog, meter, scope = captured
    before = meter.snapshot()
    with pytest.raises(Conflict, match="upstream"):
        advance(meter, scope, stage, 1, 100.0)
    assert meter.snapshot() == before
    assert catalog.watermark(*scope.key()) == 1


def test_stale_progress_and_clock_rewind_do_not_mutate_state(captured):
    _, meter, scope = captured
    advance(meter, scope, "local_spool", 3, 100.0)
    before = meter.snapshot()
    for through, now in [(2, 110.0), (4, 99.0)]:
        with pytest.raises(Conflict, match="stale"):
            advance(meter, scope, "local_spool", through, now)
        assert meter.snapshot() == before


@pytest.mark.parametrize(
    "stage,through,now",
    [
        ("ready", 1, 1.0),
        ("local_spool", -1, 1.0),
        ("local_spool", True, 1.0),
        ("local_spool", 1.5, 1.0),
        ("local_spool", 1, float("nan")),
        ("local_spool", 1, float("inf")),
        ("local_spool", 1, -1.0),
    ],
)
def test_invalid_observations_are_refused_without_mutation(captured, stage, through, now):
    _, meter, scope = captured
    before = meter.snapshot()
    with pytest.raises(ValueError):
        advance(meter, scope, stage, through, now)
    assert meter.snapshot() == before


def test_scopes_are_checked_before_reading_or_advancing(captured):
    catalog, meter, scope = captured
    advance(meter, scope, "local_spool", 3, 100.0)
    foreign = Grant("fixture-worker", "personal", GRANT.projects)
    denied = Grant("fixture-worker", GRANT.corpus, frozenset({"other-project"}))
    before = meter.snapshot()
    for grant in [foreign, denied]:
        with pytest.raises(EvidenceRefused):
            meter.status(grant, scope, 200.0)
        with pytest.raises(EvidenceRefused):
            meter.advance(grant, scope, "archive", 3, 200.0)
    assert meter.snapshot() == before
    assert meter.coverage(denied, [], 200.0)["sources"] == []
    catalog.set_state(*scope.key()[:2], "deleted", 1)
    assert meter.coverage(GRANT, [], 200.0)["sources"] == []


def test_generations_have_independent_progress(captured):
    catalog, meter, scope = captured
    next_scope = metrics.Scope(scope.access_domain, scope.session_uid, "generation-two")
    catalog.register(SessionSource(*next_scope.key(), "claude", "rotated", "retain-until-deleted"))
    advance(meter, scope, "local_spool", 3, 100.0)
    advance(meter, next_scope, "local_spool", 1, 150.0)
    assert meter.status(GRANT, scope, 200.0)["archive_lag_seconds"] == 100.0
    assert meter.status(GRANT, next_scope, 200.0)["archive_lag_seconds"] == 50.0


def test_missing_observations_are_unknown_and_never_complete(captured):
    _, meter, scope = captured
    status = meter.status(GRANT, scope, 200.0)
    assert status["coverage"] == "partial"
    assert status["alerts"] == ["archive_unknown", "embedding_unknown", "extraction_unknown"]
    assert [status[name] for name in metrics.LAG_METRICS] == [None, None, None]
    assert all(stage["state"] == "unknown" for stage in status["stages"].values())


def test_search_reveals_unacknowledged_tail_even_with_no_hits(captured, monkeypatch):
    catalog, meter, scope = captured
    advance(meter, scope, "local_spool", 3, 100.0)
    monkeypatch.setattr(metrics.time, "time", lambda: 200.0)
    answer = search.SessionSearch(catalog).search(GRANT, search.Query(GRANT.corpus, "no match"))
    assert answer["results"] == []
    assert answer["coverage"]["state"] == "partial"
    assert answer["coverage"]["sources"][0]["archive_lag_seconds"] == 100.0
    assert "private" not in json.dumps(answer["coverage"])


def test_search_project_narrowing_and_unknown_sources_do_not_claim_complete(captured):
    catalog, meter, _ = captured
    query = search.Query(GRANT.corpus, "no match", projects=("agentihooks",))
    answer = search.SessionSearch(catalog).search(GRANT, query)
    assert answer["coverage"]["state"] == "partial"
    assert len(answer["coverage"]["sources"]) == 1
    assert meter.coverage(Grant("worker", GRANT.corpus, frozenset({"other-project"})), [], 200.0) == {
        "state": "unknown",
        "sources": [],
    }


@pytest.mark.parametrize("run", [0, 1])
def test_real_capture_archive_and_embedding_boundaries(captured, tmp_path, run):
    from tests.sv2_obs02_cases import run_case

    report = run_case(tmp_path / str(run))
    assert report["archive_failed"]["alerts"] == ["archive_stale"]
    assert report["archive_failed"]["archive_lag_seconds"] == 40.0
    assert report["embedding_failed"]["archive_lag_seconds"] == 0.0
    assert report["embedding_failed"]["embedding_lag_seconds"] == 35.0
    assert report["embedding_failed"]["alerts"] == ["embedding_stale", "extraction_stale"]
    assert report["extraction_delayed"]["embedding_lag_seconds"] == 0.0
    assert report["extraction_delayed"]["extraction_lag_seconds"] == 45.0
    assert report["extraction_delayed"]["alerts"] == ["extraction_stale"]
    assert report["recovered"]["coverage"] == "complete"
    assert report["recovered"]["alerts"] == []
    assert report["first_upload_failed"]["archive_lag_seconds"] == 40.0
    assert report["first_upload_failed"]["coverage"] == "partial"
    if destination := os.environ.get("PROOF_REPORT_DIR"):
        folder = Path(destination)
        folder.mkdir(parents=True, exist_ok=True)
        (folder / f"sv2-obs02-{run}.json").write_text(json.dumps(report, indent=2, sort_keys=True))
    print("SV2-OBS-02 " + json.dumps(report, sort_keys=True))


@pytest.mark.parametrize("threshold", [0.0, -1.0, float("inf"), float("nan")])
def test_invalid_stale_thresholds_are_refused(captured, threshold):
    _, meter, scope = captured
    with pytest.raises(ValueError, match="threshold"):
        meter.status(GRANT, scope, 200.0, stale_after=threshold)


def test_unknown_generation_is_refused_and_project_narrowing_cannot_expand_grant(captured):
    _, meter, scope = captured
    unknown = metrics.Scope(scope.access_domain, scope.session_uid, "missing")
    with pytest.raises(EvidenceRefused):
        meter.advance(GRANT, unknown, "local_spool", 1, 100.0)
    with pytest.raises(EvidenceRefused):
        meter.coverage(GRANT, ["other-project"], 200.0)


def test_zero_clock_and_empty_acknowledgements_are_observed(captured):
    _, meter, scope = captured
    for stage in metrics.STAGES:
        assert advance(meter, scope, stage, 0, 0.0) is True
    status = meter.status(GRANT, scope, 0.0)
    assert status["coverage"] == "complete"
    assert all(stage["through"] == 0 and stage["updated_at"] == 0.0 for stage in status["stages"].values())
    assert status["alerts"] == []


def test_backlog_uses_first_unacknowledged_point_and_exact_stale_threshold(captured):
    _, meter, scope = captured
    for stage in metrics.STAGES:
        advance(meter, scope, stage, 1, 90.0)
    advance(meter, scope, "local_spool", 2, 100.0)
    advance(meter, scope, "local_spool", 3, 150.0)
    advance(meter, scope, "archive", 2, 160.0)
    status = meter.status(GRANT, scope, 180.0, stale_after=30.0)
    assert status["archive_lag_seconds"] == 30.0
    assert status["stages"]["archive"] == {
        "through": 2,
        "updated_at": 160.0,
        "age_seconds": 20.0,
        "backlog": 1,
        "lag_seconds": 30.0,
        "state": "stale",
    }
    assert status["alerts"] == ["archive_stale"]
    assert meter.status(GRANT, scope, 179.0, stale_after=30.0)["stages"]["archive"]["state"] == "lagging"


def test_session_inspection_displays_partial_coverage_without_changing_summary(captured, tmp_path):
    from hooks.memory.api.records import SessionTools
    from hooks.memory.archive import ObjectStore

    catalog, meter, scope = captured
    advance(meter, scope, "local_spool", 2, 100.0)
    tools = SessionTools(catalog, ObjectStore(tmp_path / "objects"))
    answer = tools.session_get(
        GRANT,
        {"schema_version": SCHEMA_VERSION, "access_domain": GRANT.corpus, "session_uid": scope.session_uid},
        200.0,
    )
    assert answer["session"]["session_uid"] == scope.session_uid
    assert answer["coverage"][0]["coverage"] == "partial"
    assert answer["coverage"][0]["archive_lag_seconds"] == 100.0


@pytest.mark.parametrize(
    "receipt",
    [
        {"state": "accepted", "through": 2},
        {"state": "committed", "through": 1},
        {"state": "committed", "through": True},
        {},
    ],
)
def test_extraction_requires_a_durable_matching_receipt(captured, receipt):
    catalog, meter, scope = captured
    for stage in metrics.STAGES:
        advance(meter, scope, stage, 1, 100.0)
    advance(meter, scope, "local_spool", 2, 110.0)
    advance(meter, scope, "archive", 2, 120.0)
    recorder = progress.Recorder(catalog, GRANT, scope, "agentihooks", clock=lambda: 200.0)
    before = meter.snapshot()
    with pytest.raises(Conflict, match="acknowledge"):
        recorder.consume(2, lambda source, through: receipt)
    assert meter.snapshot() == before


def test_extraction_replay_and_bad_cursors_never_call_the_extractor(captured):
    catalog, meter, scope = captured
    for stage in metrics.STAGES:
        advance(meter, scope, stage, 1, 100.0)
    recorder = progress.Recorder(catalog, GRANT, scope, "agentihooks", clock=lambda: 200.0)
    calls = []
    assert recorder.consume(1, lambda *args: calls.append(args)) == {"state": "replayed", "through": 1}
    for through in [0, 2, True]:
        with pytest.raises(Conflict, match="cursor"):
            recorder.consume(through, lambda *args: calls.append(args))
    assert calls == []


def test_pending_spool_replays_observations_once_after_attach_or_restart(tmp_path):
    catalog = Catalog(tmp_path / "catalog.sqlite")
    scope = metrics.Scope(GRANT.corpus, "new-session", "source")
    config = spool.SpoolConfig(tmp_path / "spool", 1000000, 800000)
    queue = spool.Spool(config, clock=lambda: 100.0)
    queue.append("one", [{"text": "private"}])
    recorder = progress.Recorder(catalog, GRANT, scope, "agentihooks", clock=lambda: 200.0)
    assert recorder.meter.status(GRANT, scope, 200.0)["coverage"] == "partial"
    queue = spool.Spool(config, progress=recorder)
    before = recorder.meter.snapshot()
    queue.append("one", [{"text": "private"}])
    assert recorder.meter.snapshot() == before
    assert recorder.meter.status(GRANT, scope, 200.0)["archive_lag_seconds"] == 100.0
    assert spool.Spool(config, progress=recorder).status().pending == [1]
    assert recorder.meter.snapshot() == before
    catalog.close()


def test_binding_cannot_expand_or_relabel_a_source(captured):
    _, meter, scope = captured
    with pytest.raises(EvidenceRefused):
        meter.bind(GRANT, scope, "other-project")
    meter.bind(GRANT, scope, "agentihooks")
    before = meter.snapshot()
    broad = Grant(GRANT.principal, GRANT.corpus, frozenset({"agentihooks", "other-project"}))
    with pytest.raises(Conflict, match="immutable"):
        meter.bind(broad, scope, "other-project")
    assert meter.snapshot() == before


def test_archive_acknowledgement_requires_a_captured_batch(captured):
    catalog, _, scope = captured
    recorder = progress.Recorder(catalog, GRANT, scope, "agentihooks", clock=lambda: 200.0)
    with pytest.raises(Conflict, match="captured batch"):
        recorder.archived(spool.Entry(1, "not-captured", 100.0, [{}], 10))


def test_metrics_never_load_transcript_records(captured, monkeypatch):
    catalog, meter, scope = captured
    monkeypatch.setattr(catalog, "events", lambda *args: pytest.fail("hydrated transcript"))
    assert meter.status(GRANT, scope, 200.0)["coverage"] == "partial"
    assert len(meter.coverage(GRANT, [], 200.0)["sources"]) == 1


def test_revoked_source_binding_cannot_be_recreated_or_resurrected(captured):
    catalog, meter, scope = captured
    meter.bind(GRANT, scope, "agentihooks")
    catalog.set_state(scope.access_domain, scope.session_uid, "deleted", 1)
    before = meter.snapshot()
    with pytest.raises(EvidenceRefused):
        meter.bind(GRANT, scope, "agentihooks")
    assert meter.snapshot() == before
    catalog.db.execute("DELETE FROM session_record WHERE access_domain = ? AND session_uid = ?", scope.key()[:2])
    assert meter.coverage(GRANT, [], 200.0)["sources"] == []


def test_a_quarantined_batch_keeps_the_archive_prefix_incomplete(tmp_path):
    catalog = Catalog(tmp_path / "catalog.sqlite")
    scope = metrics.Scope(GRANT.corpus, "gap-session", "source")
    recorder = progress.Recorder(catalog, GRANT, scope, "agentihooks", clock=lambda: 150.0)
    queue = spool.Spool(spool.SpoolConfig(tmp_path / "spool", 1000000, 800000), clock=lambda: 100.0, progress=recorder)
    queue.append("lost-first", [{"text": "first"}])
    queue.append("retained-second", [{"text": "second"}])
    sorted(queue.batches.glob("*.json"))[0].write_bytes(b"corrupt")
    result = queue.flush(lambda entry: None)
    assert result.lost == [1] and result.uploaded == [2]
    assert queue.status().pending == []
    status = recorder.meter.status(GRANT, scope, 200.0, stale_after=30.0)
    assert status["stages"]["archive"]["through"] == 0
    assert status["stages"]["archive"]["backlog"] == 2
    assert status["archive_lag_seconds"] == 100.0
    assert status["alerts"] == ["archive_stale"]
    assert status["coverage"] == "partial"
    reopened = progress.Recorder(catalog, GRANT, scope, "agentihooks")
    assert reopened.meter.status(GRANT, scope, 200.0)["stages"]["archive"]["through"] == 0
    catalog.close()


def test_inactive_replacement_cannot_clear_active_index_lag(tmp_path):
    from tests.sv2_idx02_cases import grant, records
    from tests.sv2_idx03_cases import FixtureEmbedder, Index, model
    from tests.sv2_obs02_cases import FIXTURE

    inputs = json.loads(FIXTURE.read_text())
    index = Index(inputs, tmp_path)
    caller = grant(inputs)
    scope = metrics.Scope(index.domain, inputs["session_uid"], index.session.generation)
    clock = [100.0]
    recorder = progress.Recorder(
        index.session.catalog, caller, scope, inputs["grant"]["projects"][0], clock=lambda: clock[0]
    )
    queue = spool.Spool(
        spool.SpoolConfig(tmp_path / "spool", 1000000, 800000), clock=lambda: clock[0], progress=recorder
    )

    def upload(entry):
        index.session.append([entry.records])

    queue.append("baseline", records(inputs["turns"][:1]))
    queue.flush(upload)
    index.session.build()
    index.worker.register(index.domain, index.generation, model(inputs))
    index.enqueue(100.0)
    index.drain(FixtureEmbedder(model(inputs)), 100.0)
    index.worker.activate(index.domain, index.generation, 0, now=100.0)
    before = recorder.meter.status(caller, scope, 100.0)["stages"]["indexed_chunk"]["through"]
    clock[0] = 110.0
    queue.append("tail", records(inputs["turns"][1:3]))
    queue.flush(upload)
    index.session.build()
    index.enqueue(110.0)
    index.drain(FixtureEmbedder(model(inputs), fail_calls=(1,)), 120.0)
    replacement = inputs["next_index_generation"]
    replacement_model = model(inputs, "next_model")
    index.worker.register(index.domain, replacement, replacement_model)
    index.enqueue(120.0, "next_index_generation")
    index.drain(FixtureEmbedder(replacement_model), 130.0, "next_index_generation")
    delayed = recorder.meter.status(caller, scope, 150.0)
    assert delayed["stages"]["indexed_chunk"]["through"] == before
    assert delayed["embedding_lag_seconds"] == 40.0
    assert delayed["coverage"] == "partial"
    assert index.worker.active(index.domain)["index_generation"] == index.generation
    index.worker.activate(index.domain, replacement, 1, now=160.0)
    assert recorder.meter.status(caller, scope, 170.0)["embedding_lag_seconds"] == 0.0
    index.session.catalog.close()
