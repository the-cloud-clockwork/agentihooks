"""Tick regression: a hive stopped with a recovery handoff reopens its agent's task and primes the next engineer."""

from dataclasses import replace
from typing import Any

import pytest

from scripts.handoff import envelope
from scripts.hive import registry, remove, reports
from scripts.swarm import commands, prompt
from scripts.swarm.store import AgentRecord, RedisStore
from scripts.swarm.tick import tick
from scripts.swarm_ledger.ledger_workspace import folder
from tests.swarm.test_tick import FakeRuntime, store, tasks, workers  # noqa: F401

HIVE = "anton-9"
WAIT_S = 30
STOPPED_MS = 2_000
EXIT = envelope.REASONS[7]
FRESH = {"remote_branch": "none", "remote_head": "none", "continue_from": "fresh", "fresh_reason": "none"}


class Clock:
    """A monotonic clock whose sleeps advance it, so a hive drain reaches its deadline without waiting."""

    def __init__(self) -> None:
        self.now = 0.0

    def sleep(self, seconds: float) -> None:
        self.now += seconds

    def __call__(self) -> float:
        return self.now


def _on_hive(swarm: RedisStore, agent: AgentRecord) -> AgentRecord:
    target = {"host_id": HIVE, "process_namespace": "theirs", "pid": 12, "pid_start": 34}
    return swarm.start_execution("sw", replace(agent, hive=HIVE, runtime_target=target), "")


def _stop_hive(swarm: RedisStore, monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str, str, int]]:
    built: list[tuple[str, str, str, int]] = []

    def build(given: Any, slug: str, agent: AgentRecord, reason: str, rows: Any, at: int, run: Any) -> dict[str, Any]:
        built.append((slug, agent.name, reason, at))
        return {"agent": agent.name, "task": agent.task, "reason": reason}

    monkeypatch.setattr(envelope, "build", build)
    registry.update(swarm.redis, HIVE, ["ui=no"])
    swarm.redis.sadd(registry.INDEX, HIVE)
    clock = Clock()
    host = remove.Host(sleep=clock.sleep, clock=clock, wall=lambda: STOPPED_MS / 1000)
    remove.settle(swarm, HIVE, WAIT_S, host)
    registry.beat(swarm.redis, HIVE, {"heartbeat_at": STOPPED_MS, "beat_at": STOPPED_MS})
    return built


@pytest.mark.feature("hive-stop-recovery-handoff")
def test_a_hive_stopped_with_a_recovery_handoff_reopens_the_task_and_primes_the_next_engineer(
    store: RedisStore,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(commands, "hive_id", lambda: "controller")
    monkeypatch.setattr("scripts.swarm.tick.reclaim", lambda repo, lives, recorded, heads=None: FRESH)
    ledger, runtime = tasks(("t1", "eng")), FakeRuntime()
    tick("sw", store, ledger, runtime, now_ms=1_000)
    (first,) = workers(store)
    hosted = _on_hive(store, first)
    built = _stop_hive(store, monkeypatch)
    recovery = remove.RECOVERY.format(name=hosted.name, task="t1", hive=HIVE, wait=WAIT_S, folder=folder("sw", "t1"))
    assert store.handoff("sw", "t1") == recovery
    assert built == [("sw", hosted.name, EXIT, STOPPED_MS)]

    early = tick("sw", store, ledger, runtime, now_ms=STOPPED_MS + reports.LOST_MS)
    print("DIAG early", early)
    print("DIAG config", store.config("sw"))
    print("DIAG agents", workers(store))
    late = tick("sw", store, ledger, runtime, now_ms=STOPPED_MS + reports.LOST_MS + 120_000)
    print("DIAG late", late)
    print("DIAG agents2", workers(store), runtime.spawned)

    assert not [action for action in early if action.startswith(f"retired {hosted.name}")]
    assert [agent.name for agent in workers(store)] == [hosted.name]
    assert runtime.spawned == [("eng", hosted.name, "t1")]
    assert ledger.rows["t1"]["claimed_by"] == hosted.name

    actions = tick("sw", store, ledger, runtime, now_ms=STOPPED_MS + reports.LOST_MS + 1)

    assert f"retired {hosted.name}: its hive stopped reporting, task t1 reopened" in actions
    assert runtime.spawned[-1] == ("eng", "engineer@a1b2c3-0002", "t1")
    assert ledger.rows["t1"]["claimed_by"] == "engineer@a1b2c3-0002"
    primed = runtime.tasks[-1]
    assert primed["handoff"] == recovery
    assert primed["handoff_envelope"] == {"agent": hosted.name, "task": "t1", "reason": EXIT, **FRESH}
    lines = prompt.priming_lines(primed)
    assert lines[lines.index("Handoff document: continue from it:") + 1] == recovery
    assert [agent.name for agent in workers(store)] == ["engineer@a1b2c3-0002"]
