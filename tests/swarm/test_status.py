import fakeredis

from scripts.swarm.status import findings
from scripts.swarm.store import AgentRecord, RedisStore, SwarmConfig
from scripts.swarm.tick import tick
from tests.swarm.test_tick import FakeLedger, FakeRuntime


def test_repeated_failed_launches_report_the_errors_without_a_proof_loop():
    store = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    store.create(SwarmConfig("sw", "/repo", max_eng=1, max_ci=0))
    ledger, runtime = FakeLedger([{"id": "t1", "title": "Repair launch"}]), FakeRuntime(fail=True)
    for at in range(1_000, 6_000, 1_000):
        tick("sw", store, ledger, runtime, now_ms=at)
    events = [{"kind": "task claimed", "target": "tasks/t1", "by": "swarm", "at": at} for at in range(5)]
    found = findings(store, "sw", store.config("sw"), ledger.tasks("sw"), events)
    assert [f["kind"] for f in found] == ["failed launch"]
    assert found[0]["subject"] == "t1" and "5 failed launches" in found[0]["evidence"]
    assert "herdr down" in " ".join(found[0]["evidence"])
    runtime.fail = False
    tick("sw", store, ledger, runtime, now_ms=6_000)
    assert store.claims("sw", "t1") == 1
    assert all(f["kind"] != "proof loop" for f in findings(store, "sw", store.config("sw"), ledger.tasks("sw"), events))


def test_started_lives_still_raise_the_proof_loop_finding():
    store = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    store.create(SwarmConfig("sw", "/repo", max_eng=1, max_ci=0))
    for n in range(4):
        store.record_launch("sw", AgentRecord(f"worker{n}", "eng", "t1", started_at=n), "started")
    found = findings(store, "sw", store.config("sw"), [{"id": "t1", "title": "Repair launch"}], [])
    assert [(f["kind"], f["subject"]) for f in found] == [("proof loop", "t1")]
    assert "started 4 times (3 reruns)" in found[0]["evidence"]
