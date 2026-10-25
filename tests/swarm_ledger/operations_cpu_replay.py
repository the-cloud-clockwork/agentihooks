import cProfile
import gzip
import hashlib
import io
import json
import os
import pstats
import resource
import time
from pathlib import Path

import pytest

from scripts.swarm_ledger import ledger_server as server
from scripts.swarm_ledger.api import mutations, resources, schemas
from scripts.swarm_ledger.api.errors import APIError
from scripts.swarm_ledger.events import Hub, patch
from scripts.swarm_ledger.events.publishing import Publishing
from scripts.swarm_ledger.repository.sqlite import SQLiteLedgerRepository
from tests.swarm_ledger import operations_relay

pytestmark = pytest.mark.xdist_group("fakeredis")
SLUG = "operations-replay"


def corpus():
    return json.loads(gzip.decompress(Path(__file__).with_name("operations_corpus.json.gz").read_bytes()))


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def timed(method, name, phases):
    def invoke(*args, **kwargs):
        started = time.thread_time()
        try:
            return method(*args, **kwargs)
        finally:
            phases[name] = phases.get(name, 0.0) + time.thread_time() - started

    return invoke


def replay(tmp_path, monkeypatch, profiled=False):
    recorded = corpus()
    implementation = Path(patch.__file__).resolve().parents[3]
    assert implementation == Path.cwd().resolve(), "Replay imported a different revision"
    core = server.core
    monkeypatch.setattr(core, "LEDGER_DIR", tmp_path)
    monkeypatch.setattr(core, "now_ms", lambda: 1791939600000)
    monkeypatch.setattr(server, "HUB", Hub())
    monkeypatch.setattr(server, "served_version", lambda: "replay")
    monkeypatch.setattr("hooks._redis.get_redis", lambda: None)
    monkeypatch.setattr("scripts.gates.talk.Budget._marks", lambda self: None)
    deliveries, relay_outcomes = operations_relay.install(tmp_path, monkeypatch, SLUG, server)
    repository = SQLiteLedgerRepository(tmp_path / "ledger.sqlite3", domain=core)
    repository.import_document(SLUG, recorded["state"])
    publisher = getattr(server, "publish_snapshot", server.publish_ledger)
    monkeypatch.setattr(server, "repository", Publishing(repository, publisher))
    sequence, _ = server.HUB.open(SLUG, lambda: {"ledger": server.ledger_view(repository.get_document(SLUG))})
    phases = {"request_read": 0.0}
    for instance, method, name in (
        (server.repository, "get_document", "request_read"),
        (server.repository, "current", "current_snapshot"),
        (server.repository, "apply_ops", "write_and_publish"),
        (repository, "_write", "storage_write"),
        (server.HUB, "publish_snapshot" if hasattr(server.HUB, "publish_snapshot") else "publish", "stream_publish"),
    ):
        monkeypatch.setattr(instance, method, timed(getattr(instance, method), name, phases))
    memory_before = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    outputs, seconds = [], []
    profiler = cProfile.Profile(timer=time.thread_time)
    for index, op in enumerate(recorded["ops"]):
        state = repository.get_document(SLUG)
        target = schemas.target(op)
        payload = {
            "operation_id": f"request-{index}",
            "ops": [op],
            "guards": {target: resources.resource_revision(state, target)},
        }
        if profiled:
            profiler.enable()
        start = time.thread_time()
        try:
            reply = mutations.apply(server.api_services(), SLUG, "", payload)
        except APIError as exc:
            reply = {"status": exc.status, **exc.envelope()}
        seconds.append(time.thread_time() - start)
        if profiled:
            profiler.disable()
        getattr(server.repository, "drain", lambda timeout: None)(60)
        persisted = repository.get_document(SLUG)
        frames = server.HUB.wait(SLUG, sequence, 0)
        if frames:
            sequence = frames[-1][0]
        relays = {
            name: [fingerprint(call.args) for call in callback.call_args_list] for name, callback in deliveries.items()
        }
        outputs.append(
            {
                "reply": reply,
                "state": fingerprint(persisted),
                "stream": fingerprint([(seq, name, data) for seq, _, name, data in frames]),
                "relays": relays,
                "relay_outcomes": relay_outcomes(),
            }
        )
        for callback in deliveries.values():
            callback.reset_mock()
    stream = io.StringIO()
    if profiled:
        pstats.Stats(profiler, stream=stream).sort_stats("cumulative").print_stats(25)
    return {
        "source": recorded["source"],
        "implementation": str(implementation),
        "requests": len(seconds),
        "mean_cpu_seconds": sum(seconds) / len(seconds),
        "peak_rss_kib_before_replay": memory_before,
        "peak_rss_kib_after_replay": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss,
        "cpu_seconds": seconds,
        "phase_cpu_seconds_per_request": {name: value / len(seconds) for name, value in phases.items()},
        "outputs": outputs,
        "profile": stream.getvalue(),
    }


def test_operations_write_replay_records_every_outcome(tmp_path, monkeypatch):
    with monkeypatch.context() as isolated:
        measured = replay(tmp_path / "measured", isolated)
    report = dict(measured)
    if os.environ.get("OPERATIONS_CPU_PROFILE"):
        with monkeypatch.context() as isolated:
            profiled = replay(tmp_path / "profiled", isolated, profiled=True)
        assert measured["outputs"] == profiled["outputs"]
        report["profile"] = profiled["profile"]
    if destination := os.environ.get("OPERATIONS_CPU_REPORT"):
        Path(destination).write_text(json.dumps(report, indent=2) + "\n")
    assert measured["requests"] == len(corpus()["ops"])
