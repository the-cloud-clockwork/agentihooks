"""A hive member that restarted ends the join reports its agents wrote before the restart, since they died with it."""

import json
from dataclasses import replace
from typing import TYPE_CHECKING, Any

import pytest

from tests.hive.test_join_report import passing

if TYPE_CHECKING:
    from scripts.swarm.store import AgentRecord, RedisStore

RESTARTED = {"exit_reason": "agent ended with its member's restart", "status": "ended"}


@pytest.fixture
def store() -> "RedisStore":
    from fakeredis import FakeRedis

    from scripts.swarm.store import RedisStore, SwarmConfig

    store = RedisStore(FakeRedis(decode_responses=True))
    store.create(SwarmConfig("swarm", "repo", 8, 0))
    return store


def _running(store: "RedisStore", seat: str) -> "AgentRecord":
    from scripts.swarm.store import AgentRecord

    agent = AgentRecord(store.next_name("swarm", "eng"), "eng", "task", runtime_target={"host_id": "worker"})
    agent = store.start_execution("swarm", replace(agent, seat=f"{seat}@swarm"))
    store.put_agent("swarm", agent)
    return agent


def _reported(store: "RedisStore", agent: "AgentRecord", recorded: dict[str, str]) -> None:
    from scripts.hive import join_report

    report = join_report.graded(
        {
            "agent": agent.name,
            "slug": "swarm",
            "task": agent.task,
            "execution_id": agent.execution_id,
            "generation": agent.generation,
            "session_id": f"session {agent.name}",
            "started_at": 1000,
            "heartbeat_at": 1000,
            "status": "running",
            "checks": passing(),
            "context": {},
            **recorded,
        }
    )
    store.redis.hset(join_report.key("worker"), agent.execution_id, json.dumps(report))


def _after(store: "RedisStore") -> dict[str, dict[str, Any]]:
    from scripts.hive import join_report

    return {report["agent"]: report for report in join_report.listing(store.redis, "worker")}


@pytest.mark.feature("hive-join-heartbeat-ends-restarted-member-reports")
def test_a_restarted_member_ends_the_reports_its_agents_wrote_before_the_restart(
    store: "RedisStore", monkeypatch: pytest.MonkeyPatch
) -> None:
    from scripts.hive import join_report, registry

    monkeypatch.setattr(registry, "member_name", lambda: "pod-a")
    monkeypatch.setattr(join_report, "local_epoch", lambda: "boot/2", raising=False)
    monkeypatch.setattr(join_report, "cheap_checks", lambda *args: {})
    before, other, unrecorded, current = (_running(store, f"eng-{seat}") for seat in range(1, 5))
    _reported(store, before, {"member": "pod-a", "epoch": "boot/1"})
    _reported(store, other, {"member": "pod-b", "epoch": "boot/1"})
    _reported(store, unrecorded, {})
    join_report.start(store.redis, "swarm", current, "session current", passing(), {}, 1000)
    join_report.heartbeat(store.redis, "worker", 1001)
    after = _after(store)
    assert {key: after[before.name].get(key) for key in ("ended_at", *RESTARTED)} == {"ended_at": 1001, **RESTARTED}
    for agent in (other, unrecorded, current):
        assert after[agent.name]["status"] == "running"
        assert "ended_at" not in after[agent.name]
    live = {report["agent"] for report in join_report.live(store.redis, "worker")}
    assert live == {other.name, unrecorded.name, current.name}
