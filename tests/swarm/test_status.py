import pytest

from scripts.swarm.status import findings
from scripts.swarm.store import AgentRecord, RedisStore, SwarmConfig
from scripts.swarm.tick import tick
from tests.swarm.test_tick import FakeLedger, FakeRuntime

pytestmark = pytest.mark.xdist_group("fakeredis")


def test_repeated_failed_launches_report_the_errors_without_a_proof_loop():
    import fakeredis

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
    import fakeredis

    store = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    store.create(SwarmConfig("sw", "/repo", max_eng=1, max_ci=0))
    for n in range(4):
        store.record_launch("sw", AgentRecord(f"worker{n}", "eng", "t1", started_at=n), "started")
    found = findings(store, "sw", store.config("sw"), [{"id": "t1", "title": "Repair launch"}], [])
    assert [(f["kind"], f["subject"]) for f in found] == [("proof loop", "t1")]
    assert "started 4 times (3 reruns)" in found[0]["evidence"]


def test_busy_tool_stall_is_reported_until_a_new_tool_call_or_named_wait(monkeypatch, tmp_path):
    import fakeredis

    from scripts.swarm import idle
    from scripts.swarm.health import activity
    from scripts.swarm.store import AgentRecord

    store = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    config = SwarmConfig("sw", "/repo", max_eng=1, max_ci=0)
    store.create(config)
    at = 1_000_000
    name = "engineer@a1b2c3-0001"
    store.put_agent("sw", AgentRecord(name, "eng", "t1", started_at=at - 11 * 60_000))
    monkeypatch.setattr(activity, "default_root", lambda: tmp_path)
    monkeypatch.setattr("scripts.swarm.status.now_ms", lambda: at)
    monkeypatch.setattr("scripts.swarm.status.live_binding.findings", lambda *a: [])
    monkeypatch.setattr("scripts.swarm.status.retire_watch.findings", lambda *a: [])
    monkeypatch.setattr("scripts.swarm.status.launch_check.findings", lambda *a: [])
    ledger, runtime = FakeLedger([{"id": "t1", "state": "claimed", "claimed_by": name}]), FakeRuntime()
    runtime.live.add(name)
    env = {"AGENTIHOOKS_SWARM": "sw", "AGENTIHOOKS_AGENT_NAME": name}
    activity.record("Read", {}, env, tmp_path, now_ms=at - 10 * 60_000)
    from scripts.swarm.tick import _watch_idle

    _watch_idle("sw", store, ledger, runtime, ledger.tasks("sw"), store.agents("sw")[0], at)
    assert store.redis.get(store.key("sw", "pane-state", name)) == "working"
    found = findings(store, "sw", config, ledger.tasks("sw"), [])
    assert "stalled" in [f["kind"] for f in found]
    idle.declare_wait(store.redis, "sw", name, at + 60_000, "checks", at)
    assert "stalled" not in [f["kind"] for f in findings(store, "sw", config, ledger.tasks("sw"), [])]
    idle.end_wait(store.redis, "sw", name, at)
    activity.record("Read", {}, env, tmp_path, now_ms=at)
    assert "stalled" not in [f["kind"] for f in findings(store, "sw", config, ledger.tasks("sw"), [])]
    assert runtime.nudged == [] and runtime.killed == []


def test_stall_report_respects_actual_launch_and_startup_grace(monkeypatch, tmp_path):
    import fakeredis

    from scripts.swarm.health import activity
    from scripts.swarm.health import findings as health
    from scripts.swarm.status import _health_rows
    from scripts.swarm.store import AgentRecord

    store = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    monkeypatch.setattr(activity, "default_root", lambda: tmp_path)
    at = 1_000_000
    name = "engineer@a1b2c3-0001"
    limits = health.limits({"AGENTIHOOKS_HEALTH_STALLED_MINUTES": "4"})
    worker = AgentRecord(name, "eng", "t1", started_at=at - 11 * 60_000, launched_at=at - 4 * 60_000)
    store.redis.set(store.key("sw", "pane-state", name), "working")
    rows = _health_rows(store, "sw", [worker], {}, at)
    assert health.stalled(rows, limits) == []
    rows = _health_rows(store, "sw", [worker], {}, at + 3 * 60_000)
    assert [f.kind for f in health.stalled(rows, limits)] == ["stalled"]
