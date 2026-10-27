import json
from pathlib import Path

from hooks.memory import metrics, search, spool
from tests.sv2_idx02_cases import grant, records
from tests.sv2_idx03_cases import FixtureEmbedder, Index, model

FIXTURE = Path(__file__).parent / "fixtures/swarm_v2/session-embeddings.json"


def run_case(root):
    inputs = json.loads(FIXTURE.read_text())
    index = Index(inputs, root)
    session, caller = index.session, grant(inputs)
    scope = metrics.Scope(index.domain, inputs["session_uid"], session.generation)
    meter = metrics.Metrics(session.catalog)
    queue = spool.Spool(spool.SpoolConfig(root / "spool", max_bytes=1000000, warn_bytes=800000), clock=lambda: 110.0)
    session.append(inputs["turns"][:1])
    session.build()
    index.worker.register(index.domain, index.generation, model(inputs))
    index.enqueue(100.0)
    index.drain(FixtureEmbedder(model(inputs)), 100.0)
    for stage in metrics.STAGES:
        meter.advance(caller, scope, stage, session.sequence, 100.0)
    tail = inputs["turns"][1:3]
    through = session.sequence + len(records(tail))
    queue.append("fixture-tail", records(tail))
    meter.advance(caller, scope, "local_spool", through, 110.0)
    baseline = session.protected()

    def unavailable(entry):
        raise ConnectionError("fixture archive transport unavailable")

    failed = queue.flush(unavailable)
    assert failed.error == "ConnectionError"
    assert queue.status().pending == [1]
    assert session.protected() == baseline
    archive_failed = meter.status(caller, scope, 150.0, stale_after=30.0)

    def upload(entry):
        assert entry.records == records(tail)
        assert session.append(tail)["state"] == "committed"
        meter.advance(caller, scope, "archive", session.sequence, 155.0)

    assert queue.flush(upload).uploaded == [1]
    session.build()
    index.enqueue(155.0)
    deferred = index.drain(FixtureEmbedder(model(inputs), fail_calls=(1,)), 160.0)
    assert deferred["state"] == "deferred"
    assert index.coverage()["state"] == "incomplete"
    embedding_failed = meter.status(caller, scope, 190.0, stale_after=30.0)
    assert index.drain(FixtureEmbedder(model(inputs)), 195.0)["state"] == "drained"
    assert index.coverage()["state"] == "indexed"
    meter.advance(caller, scope, "indexed_chunk", session.sequence, 195.0)
    extraction_delayed = meter.status(caller, scope, 200.0, stale_after=30.0)
    answer = search.SessionSearch(session.catalog).search(caller, search.Query(index.domain, "backlog"))
    assert answer["coverage"]["state"] == "partial"
    before_restart = meter.snapshot()
    index.restart()
    meter = metrics.Metrics(index.session.catalog)
    assert meter.snapshot() == before_restart
    meter.advance(caller, scope, "brain_consumed", index.session.sequence, 210.0)
    recovered = meter.status(caller, scope, 220.0, stale_after=30.0)
    rollback = meter.snapshot()
    index.worker.set_paused(index.domain, index.generation, True)
    assert metrics.Metrics(index.session.catalog).snapshot() == rollback
    index.session.catalog.close()
    return {
        "archive_failed": archive_failed,
        "embedding_failed": embedding_failed,
        "extraction_delayed": extraction_delayed,
        "recovered": recovered,
        "restart_retained_watermarks": True,
        "rollback_retained_watermarks": True,
        "evidence_class": "mocked transport and embedder; real local spool, catalog, archive objects and index worker",
    }
