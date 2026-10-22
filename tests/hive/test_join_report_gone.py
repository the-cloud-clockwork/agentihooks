import json
from dataclasses import replace

import pytest

ENDED = {"exit_reason": "agent gone from the swarm", "status": "ended"}


def passing():
    from scripts.hive import join_report

    return dict.fromkeys((*join_report.REQUIRED, *join_report.OPTIONAL), True)


@pytest.fixture
def store():
    from fakeredis import FakeRedis

    from scripts.swarm.store import RedisStore, SwarmConfig

    store = RedisStore(FakeRedis(decode_responses=True))
    for slug in ("swarm", "other"):
        store.create(SwarmConfig(slug, "repo", 8, 0))
    return store


def running(store, slug, seat):
    from scripts.swarm.store import AgentRecord

    agent = AgentRecord(store.next_name(slug, "eng"), "eng", "task", runtime_target={"host_id": "worker"})
    agent = store.start_execution(slug, replace(agent, seat=f"{seat}@{slug}"))
    store.put_agent(slug, agent)
    return agent


def test_heartbeat_ends_a_failed_launch_report_whose_agent_is_gone(store, monkeypatch):
    from scripts.hive import join_report
    from scripts.swarm.store import AgentRecord

    agent = AgentRecord("engineer", "eng", "task", execution_id="execution", generation=2, hive="worker")
    checks = {**passing(), "pane": False, "transcript": False}
    started = join_report.start(store.redis, "swarm", agent, "session", checks, {}, 1000)
    assert started["grade"] == "failed"

    def probe(*args):
        pytest.fail("a gone agent's report must not be probed")

    monkeypatch.setattr(join_report, "cheap_checks", probe)
    join_report.heartbeat(store.redis, "worker", 1001)
    ended = {**started, "ended_at": 1001, **ENDED}
    assert json.loads(store.redis.hget(join_report.key("worker"), "execution")) == ended
    join_report.heartbeat(store.redis, "worker", 900000)
    assert json.loads(store.redis.hget(join_report.key("worker"), "execution")) == ended


def test_heartbeat_ends_only_reports_of_executions_their_swarm_no_longer_runs(store, monkeypatch):
    from scripts.hive import join_report

    kept = running(store, "swarm", "eng-1")
    elsewhere = running(store, "other", "eng-1")
    reports = {
        "kept": (kept, "swarm"),
        "old generation": (replace(kept, execution_id="old", generation=kept.generation + 1), "swarm"),
        "wrong swarm": (elsewhere, "swarm"),
    }
    for label, (agent, slug) in reports.items():
        join_report.start(store.redis, slug, replace(agent, name=label), "session", passing(), {}, 1000)
    monkeypatch.setattr(join_report, "cheap_checks", lambda *args: {})
    join_report.heartbeat(store.redis, "worker", 1001)
    after = {report["agent"]: report for report in join_report.listing(store.redis, "worker")}
    assert after["kept"]["status"] == "running"
    assert "ended_at" not in after["kept"]
    for label in ("old generation", "wrong swarm"):
        assert {name: after[label][name] for name in ("ended_at", *ENDED)} == {"ended_at": 1001, **ENDED}
