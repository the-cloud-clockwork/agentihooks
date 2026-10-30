from dataclasses import replace

import fakeredis
import pytest

from scripts.swarm import commands
from scripts.swarm.store import RedisStore, SwarmConfig
from scripts.swarm.tick import STARTUP_GRACE_MS, tick
from tests.swarm.test_tick import FakeRuntime, tasks, workers


@pytest.fixture
def store() -> RedisStore:
    s = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    s.create(SwarmConfig("sw", "/repo", max_eng=2, max_ci=1))
    return s


@pytest.mark.parametrize(("hive", "kept"), [("anton-2", True), ("hub", False)])
def test_a_hub_admitted_agent_on_another_hive_outlives_the_local_startup_grace(
    store: RedisStore, monkeypatch: pytest.MonkeyPatch, hive: str, kept: bool
) -> None:
    monkeypatch.setattr(commands, "hive_id", lambda: "hub")
    ledger, runtime = tasks(("t1", "eng")), FakeRuntime()
    tick("sw", store, ledger, runtime, now_ms=1_000)
    (agent,) = workers(store)
    store.put_agent("sw", replace(agent, hive=hive, runtime_target={}))
    runtime.live.clear()
    runtime.statuses[agent.name] = "unknown"
    actions = tick("sw", store, ledger, runtime, now_ms=1_000 + STARTUP_GRACE_MS + 1)
    if kept:
        assert ledger.rows["t1"]["claimed_by"] == agent.name and runtime.spawned == [("eng", agent.name, "t1")]
        assert [a.state for a in workers(store)] == ["suspect"]
        assert f"suspect {agent.name}: its runtime did not answer" in actions
    else:
        assert any(action.startswith(f"lost {agent.name}") for action in actions)
        assert runtime.spawned[-1] == ("eng", "engineer@a1b2c3-0002", "t1")
