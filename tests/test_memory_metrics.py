import json

import pytest

from hooks.memory import metrics, search
from hooks.memory.api.ingest import Grant
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
        BatchCommit(GRANT.corpus, "batch-one", "digest", scope.session_uid, scope.source_generation, [event], [], "1.0")
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
    answer = tools.session_get(GRANT, {"access_domain": GRANT.corpus, "session_uid": scope.session_uid}, 200.0)
    assert answer["session"]["session_uid"] == scope.session_uid
    assert answer["coverage"][0]["coverage"] == "partial"
    assert answer["coverage"][0]["archive_lag_seconds"] == 100.0
