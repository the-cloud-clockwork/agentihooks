import pytest

from scripts.gates import log
from scripts.gates.base import Who
from scripts.gates.enforcement import Enforcement
from scripts.swarm.store import RedisStore, SwarmConfig

pytestmark = pytest.mark.unit


@pytest.mark.parametrize("configured", [None, "observe", "unknown"])
@pytest.mark.parametrize("action", ["stop", "reopen", "block"])
def test_new_enforcement_logs_each_action_without_enforcing(configured, action, tmp_path, monkeypatch):
    import fakeredis

    monkeypatch.setattr(log, "swarm_home", lambda: tmp_path)
    store = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    store.create(SwarmConfig("demo", "/repo", 1, 1, gates={"new-rule": configured}))
    who = Who(name="worker", swarm="demo", task="task")
    monkeypatch.setenv("AGENTIHOOKS_GATE_NEW_RULE", "enforce")
    assert Enforcement("new-rule").allows(store, who, action, "stalled", now_ms=1234) is False
    assert log.recent("demo") == [
        {
            "at": 1234,
            "gate": "new-rule",
            "kind": "observe",
            "agent": "worker",
            "task": "task",
            "tool": action,
            "reason": "stalled",
        }
    ]


@pytest.mark.parametrize("configured, allowed, kind", [("enforce", True, "deny"), ("off", False, None)])
def test_swarm_config_selects_enforcement_and_skip(configured, allowed, kind, tmp_path, monkeypatch):
    import fakeredis

    monkeypatch.setattr(log, "swarm_home", lambda: tmp_path)
    store = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    store.create(SwarmConfig("demo", "/repo", 1, 1, gates={"new-rule": configured}))
    who = Who(name="worker", swarm="demo", task="task")
    monkeypatch.setenv("AGENTIHOOKS_GATE_NEW_RULE", "observe")
    assert Enforcement("new-rule").allows(store, who, "stop", "stalled", now_ms=1234) is allowed
    assert [row["kind"] for row in log.recent("demo")] == ([] if kind is None else [kind])
