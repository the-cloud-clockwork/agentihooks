import json
from pathlib import Path

from hooks.memory import metrics, search, spool
from tests.sv2_idx02_cases import grant, records
from tests.sv2_idx03_cases import FixtureEmbedder, Index, model

FIXTURE = Path(__file__).parent / "fixtures/swarm_v2/session-embeddings.json"


def run_case(root):
    from hooks.memory.progress import Recorder

    inputs = json.loads(FIXTURE.read_text())
    index = Index(inputs, root)
    session, caller = index.session, grant(inputs)
    scope = metrics.Scope(index.domain, inputs["session_uid"], session.generation)
    clock = [100.0]
    recorder = Recorder(session.catalog, caller, scope, inputs["grant"]["projects"][0], clock=lambda: clock[0])
    meter = recorder.meter
    queue = spool.Spool(spool.SpoolConfig(root / "spool", 1000000, 800000), clock=lambda: clock[0], progress=recorder)

    def upload(entry):
        assert session.append([entry.records])["state"] == "committed"

    def committed(source, through):
        assert source == scope
        return {"state": "committed", "through": through}

    def unavailable(entry):
        raise ConnectionError("fixture archive transport unavailable")

    queue.append("fixture-baseline", records(inputs["turns"][:1]))
    assert queue.flush(upload).uploaded == [1]
    session.build()
    index.worker.register(index.domain, index.generation, model(inputs))
    index.enqueue(100.0)
    index.drain(FixtureEmbedder(model(inputs)), 100.0)
    recorder.consume(session.sequence, committed)
    clock[0] = 110.0
    queue.append("fixture-tail", records(inputs["turns"][1:3]))
    baseline = session.protected()
    assert queue.flush(unavailable).error == "ConnectionError"
    assert queue.status().pending == [2]
    assert session.protected() == baseline
    archive_failed = meter.status(caller, scope, 150.0, stale_after=30.0)
    clock[0] = 155.0
    assert queue.flush(upload).uploaded == [2]
    session.build()
    index.enqueue(155.0)
    assert index.drain(FixtureEmbedder(model(inputs), fail_calls=(1,)), 160.0)["state"] == "deferred"
    assert index.coverage()["state"] == "incomplete"
    embedding_failed = meter.status(caller, scope, 190.0, stale_after=30.0)
    assert index.drain(FixtureEmbedder(model(inputs)), 195.0)["state"] == "drained"
    assert index.coverage()["state"] == "indexed"
    extraction_delayed = meter.status(caller, scope, 200.0, stale_after=30.0)
    answer = search.SessionSearch(session.catalog).search(caller, search.Query(index.domain, "backlog"))
    assert answer["coverage"]["state"] == "partial"
    before_restart = meter.snapshot()
    index.restart()
    session = index.session
    recorder = Recorder(session.catalog, caller, scope, inputs["grant"]["projects"][0], clock=lambda: clock[0])
    meter = recorder.meter
    assert meter.snapshot() == before_restart
    clock[0] = 210.0
    recorder.consume(session.sequence, committed)
    recovered = meter.status(caller, scope, 220.0, stale_after=30.0)
    rollback = meter.snapshot()
    index.worker.set_paused(index.domain, index.generation, True)
    assert metrics.Metrics(session.catalog).snapshot() == rollback
    first_scope = metrics.Scope(index.domain, "first-upload-fails", "new-source")
    first = Recorder(session.catalog, caller, first_scope, inputs["grant"]["projects"][0], clock=lambda: 100.0)
    first_queue = spool.Spool(
        spool.SpoolConfig(root / "first-spool", 1000000, 800000), clock=lambda: 110.0, progress=first
    )
    first_queue.append("first", [{"kind": "user_text", "text": "fixture private tail"}])
    assert first_queue.flush(unavailable).error == "ConnectionError"
    assert session.catalog.session(index.domain, first_scope.session_uid) is None
    first_failed = meter.status(caller, first_scope, 150.0, stale_after=30.0)
    assert meter.coverage(caller, [], 220.0)["state"] == "partial"
    session.catalog.close()
    return {
        "archive_failed": archive_failed,
        "embedding_failed": embedding_failed,
        "extraction_delayed": extraction_delayed,
        "recovered": recovered,
        "first_upload_failed": first_failed,
        "restart_retained_watermarks": True,
        "rollback_retained_watermarks": True,
        "evidence_class": "mocked transport, extractor and embedder; real local spool, catalog, archive objects and index worker",
    }
