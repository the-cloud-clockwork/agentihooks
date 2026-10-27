import json

import pytest

from hooks.memory import archive, index_worker, metrics, progress, spool
from hooks.memory.api.ingest import SCHEMA_VERSION, Grant
from hooks.memory.catalog import BatchCommit, Conflict, StoredEvent
from tests.test_memory_metrics import GRANT, advance
from tests.test_memory_metrics import captured as captured

pytestmark = pytest.mark.unit


@pytest.mark.parametrize("clock", [-1.0, float("inf"), float("nan")])
def test_clock_error_contract(captured, clock):
    _, meter, scope = captured
    with pytest.raises(ValueError, match="^invalid observation clock$"):
        advance(meter, scope, "local_spool", 1, clock)


@pytest.mark.parametrize("through", [-1, True, 1.5, 9223372036854775808])
def test_cursor_error_contract(captured, through):
    _, meter, scope = captured
    with pytest.raises(ValueError, match="^invalid stage watermark$"):
        advance(meter, scope, "local_spool", through, 100.0)


def test_largest_sqlite_cursor_is_accepted(captured):
    _, meter, scope = captured
    assert advance(meter, scope, "local_spool", 9223372036854775807, 100.0)
    assert meter.status(GRANT, scope, 100.0)["stages"]["local_spool"]["through"] == 9223372036854775807


def test_exact_status_metadata_and_complete_coverage(captured):
    _, meter, scope = captured
    for stage in metrics.STAGES:
        advance(meter, scope, stage, 1, 100.0)
    status = meter.status(GRANT, scope, 100.0)
    assert status["session_uid"] == scope.session_uid
    assert status["source_generation"] == scope.source_generation
    assert all(stage["age_seconds"] == 0.0 for stage in status["stages"].values())
    assert meter.coverage(GRANT, [], 100.0) == {"state": "complete", "sources": [status]}
    assert set(meter.snapshot()) == {"memory_stage_progress", "memory_progress_source", "memory_progress_batch"}
    advance(meter, scope, "local_spool", 2, 110.0)
    assert meter.status(GRANT, scope, 110.0)["archive_lag_seconds"] == 0.0
    assert meter.status(GRANT, scope, 409.0)["alerts"] == []
    assert meter.status(GRANT, scope, 410.0)["alerts"] == ["archive_stale"]
    assert meter._status(scope, 410.0)["alerts"] == ["archive_stale"]
    assert meter.status(GRANT, scope, 110.0, stale_after=0.5)["alerts"] == []


def test_zero_upstream_without_acknowledgement_remains_unknown(captured):
    _, meter, scope = captured
    advance(meter, scope, "local_spool", 0, 0.0)
    stage = meter.status(GRANT, scope, 0.0)["stages"]["archive"]
    assert stage == {
        "through": None,
        "updated_at": None,
        "age_seconds": None,
        "backlog": 0,
        "lag_seconds": None,
        "state": "unknown",
    }


def test_upstream_cursor_is_not_its_timestamp(captured):
    _, meter, scope = captured
    advance(meter, scope, "local_spool", 1, 100.0)
    with pytest.raises(Conflict, match="^stage acknowledgement exceeds observed upstream$"):
        advance(meter, scope, "archive", 2, 110.0)


def test_refusal_codes_and_foreign_event_binding(captured):
    catalog, meter, scope = captured
    foreign_domain = Grant(GRANT.principal, "other", GRANT.projects)
    with pytest.raises(archive.EvidenceRefused, match=f"^{archive.FORBIDDEN}$"):
        meter.status(foreign_domain, scope, 100.0)
    with pytest.raises(archive.EvidenceRefused, match=f"^{archive.FORBIDDEN}$"):
        meter.bind(foreign_domain, scope, "agentihooks")
    with pytest.raises(archive.EvidenceRefused, match=f"^{archive.FORBIDDEN}$"):
        meter.coverage(GRANT, ["foreign"], 100.0)
    missing = metrics.Scope(scope.access_domain, scope.session_uid, "missing")
    with pytest.raises(archive.EvidenceRefused, match=f"^{archive.UNAVAILABLE}$"):
        meter.status(GRANT, missing, 100.0)
    foreign_project = Grant(GRANT.principal, GRANT.corpus, frozenset({"other-project"}))
    with pytest.raises(archive.EvidenceRefused, match=f"^{archive.UNAVAILABLE}$"):
        meter.bind(foreign_project, scope, "other-project")
    event = StoredEvent("foreign-event", 2, 1, 2, "other-project", {"kind": "user_text"}, "hash-two")
    catalog.commit(
        BatchCommit(*scope.key()[:1], "foreign-batch", "digest-two", *scope.key()[1:], [event], [], SCHEMA_VERSION)
    )
    before = meter.snapshot()
    with pytest.raises(archive.EvidenceRefused, match=f"^{archive.UNAVAILABLE}$"):
        meter.bind(GRANT, scope, "agentihooks")
    assert meter.snapshot() == before
    catalog.set_state(scope.access_domain, scope.session_uid, "deleted", 1)
    with pytest.raises(archive.EvidenceRefused, match=f"^{archive.UNAVAILABLE}$"):
        meter.bind(GRANT, scope, "agentihooks")


def test_extraction_without_observations_is_replay_or_refusal(captured):
    catalog, _, scope = captured
    recorder = progress.Recorder(catalog, GRANT, scope, "agentihooks")
    calls = []
    assert recorder.consume(0, lambda *args: calls.append(args)) == {"state": "replayed", "through": 0}
    with pytest.raises(Conflict, match="^extraction cursor is stale or exceeds archive$"):
        recorder.consume(1, lambda *args: calls.append(args))
    assert calls == []


def test_spool_attachment_takes_the_canonical_lock(captured, tmp_path):
    catalog, _, scope = captured
    recorder = progress.Recorder(catalog, GRANT, scope, "agentihooks")
    root = tmp_path / "spool"
    spool.Spool(spool.SpoolConfig(root, 1000000, 800000), progress=recorder)
    assert {path.name for path in root.glob(".*.lock")} == {".spool.lock"}


def test_initial_capture_zeros_and_duplicate_archive_ignore_stale_clock(captured):
    catalog, meter, scope = captured
    clock = [150.0]
    recorder = progress.Recorder(catalog, GRANT, scope, "agentihooks", clock=lambda: clock[0])
    entry = spool.Entry(1, "one", 100.0, [{}], 10)
    recorder.buffered(entry)
    status = meter.status(GRANT, scope, 100.0)
    for stage in metrics.STAGES[1:]:
        assert status["stages"][stage]["through"] == 0
        assert status["stages"][stage]["updated_at"] == 0.0
    recorder.archived(entry)
    before = meter.snapshot()
    clock[0] = 0.0
    recorder.archived(entry)
    assert meter.snapshot() == before


def test_reconciliation_preserves_tracked_gap_and_duplicate_clock(tmp_path):
    from tests.sv2_idx02_cases import grant, records
    from tests.sv2_idx03_cases import Index
    from tests.sv2_obs02_cases import FIXTURE

    inputs = json.loads(FIXTURE.read_text())
    index = Index(inputs, tmp_path)
    index.session.append(inputs["turns"][:1])
    scope = metrics.Scope(index.domain, inputs["session_uid"], index.session.generation)
    caller = grant(inputs)
    clock = [100.0]
    recorder = progress.Recorder(
        index.session.catalog, caller, scope, inputs["grant"]["projects"][0], clock=lambda: clock[0]
    )
    baseline = index.session.sequence
    entry = spool.Entry(1, "ambiguous-upload", 100.0, records(inputs["turns"][1:3]), 10)
    recorder.buffered(entry)
    index.session.append([entry.records])
    assert index.session.sequence > baseline
    assert recorder.reconcile(index.session.store) == baseline
    initial = recorder.meter.status(caller, scope, 100.0)
    assert initial["stages"]["archive"]["through"] == baseline
    assert initial["stages"]["archive"]["backlog"] == len(entry.records)
    assert initial["coverage"] == "partial"
    before = recorder.meter.snapshot()
    clock[0] = 0.0
    assert recorder.reconcile(index.session.store) == baseline
    assert recorder.meter.snapshot() == before
    clock[0] = 150.0
    recorder.archived(entry)
    assert recorder.meter.status(caller, scope, 150.0)["archive_lag_seconds"] == 0.0
    index.session.catalog.close()


def test_initial_empty_reconciliation_records_exact_zero_points(captured, monkeypatch):
    catalog, meter, scope = captured
    recorder = progress.Recorder(catalog, GRANT, scope, "agentihooks", clock=lambda: 100.0)
    monkeypatch.setattr(
        archive, "evidence", lambda *args: {"sources": [{"source_generation": scope.source_generation, "events": []}]}
    )
    assert recorder.reconcile(None) == 0
    status = meter.status(GRANT, scope, 100.0)
    for stage in metrics.STAGES[1:]:
        assert status["stages"][stage]["through"] == 0
        assert status["stages"][stage]["updated_at"] == 0.0


def test_index_cursor_boundaries_and_replay(captured, monkeypatch):
    catalog, meter, scope = captured
    index_worker.IndexWorker(catalog)
    catalog.db.execute("INSERT INTO index_cutover VALUES (?, ?, ?)", (scope.access_domain, "active", 1))
    meter.bind(GRANT, scope, "agentihooks")
    query = """SELECT '{"generation-one":1}' WHERE ? IS NOT NULL AND ? IS NOT NULL AND ? IS NOT NULL"""
    monkeypatch.setattr(progress, "COMPLETE", query)
    progress.indexed(catalog, scope.access_domain, "active", 120.0)
    assert meter.snapshot()["memory_stage_progress"] == []
    advance(meter, scope, "local_spool", 3, 100.0)
    advance(meter, scope, "archive", 3, 110.0)
    monkeypatch.undo()
    progress.indexed(catalog, scope.access_domain, "active", 120.0)
    assert meter.status(GRANT, scope, 120.0)["stages"]["indexed_chunk"]["through"] is None
    monkeypatch.setattr(progress, "COMPLETE", "SELECT '{}' WHERE ? IS NOT NULL AND ? IS NOT NULL AND ? IS NOT NULL")
    progress.indexed(catalog, scope.access_domain, "active", 120.0)
    assert meter.status(GRANT, scope, 120.0)["stages"]["indexed_chunk"]["through"] is None
    monkeypatch.setattr(progress, "COMPLETE", query)
    progress.indexed(catalog, scope.access_domain, "active", 120.0)
    assert meter.status(GRANT, scope, 120.0)["stages"]["indexed_chunk"]["through"] == 1
    before = meter.snapshot()
    progress.indexed(catalog, scope.access_domain, "active", 0.0)
    assert meter.snapshot() == before


def test_archive_acknowledgements_advance_only_the_contiguous_prefix(captured):
    catalog, meter, scope = captured
    recorder = progress.Recorder(catalog, GRANT, scope, "agentihooks", clock=lambda: 150.0)
    for stage in metrics.STAGES:
        meter._advance(scope, stage, 0, 0.0)
    entries = [spool.Entry(i, str(i), 100.0, [{}], 10) for i in range(1, 4)]
    for entry in entries:
        recorder.buffered(entry)
    recorder.archived(entries[1])
    assert meter.status(GRANT, scope, 150.0)["stages"]["archive"]["through"] == 0
    recorder.archived(entries[0])
    assert meter.status(GRANT, scope, 150.0)["stages"]["archive"]["through"] == 2
    assert meter.status(GRANT, scope, 150.0)["stages"]["archive"]["backlog"] == 1


def test_reconciliation_stops_at_a_real_archive_sequence_hole(tmp_path):
    from tests.sv2_idx02_cases import grant
    from tests.sv2_idx03_cases import Index
    from tests.sv2_obs02_cases import FIXTURE

    inputs = json.loads(FIXTURE.read_text())
    index = Index(inputs, tmp_path)
    index.session.append(inputs["turns"][:1])
    baseline = index.session.sequence
    index.session.sequence += 1
    index.session.append(inputs["turns"][1:2])
    scope = metrics.Scope(index.domain, inputs["session_uid"], index.session.generation)
    caller = grant(inputs)
    recorder = progress.Recorder(
        index.session.catalog, caller, scope, inputs["grant"]["projects"][0], clock=lambda: 100.0
    )
    assert index.session.catalog.watermark(*scope.key()) == baseline
    assert recorder.reconcile(index.session.store) == baseline
    status = recorder.meter.status(caller, scope, 100.0)
    assert status["stages"]["archive"]["through"] == baseline
    for stage in ("indexed_chunk", "brain_consumed"):
        assert status["stages"][stage]["through"] == 0
        assert status["stages"][stage]["updated_at"] == 0.0
    index.session.catalog.close()
