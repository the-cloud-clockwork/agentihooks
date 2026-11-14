from scripts.swarm.tick import tick
from tests.swarm.test_tick import FakeRuntime, store, tasks  # noqa: F401


def test_an_events_engine_swarm_leaves_task_spawns_to_hive_pulls(store):  # noqa: F811
    store.update("sw", engine="events")
    ledger, runtime = tasks(("t1", "eng"), ("t2", "eng")), FakeRuntime()
    tick("sw", store, ledger, runtime, now_ms=1_000)
    assert [task for _, _, task in runtime.spawned] == []
    assert [row["state"] for row in ledger.rows.values()] == ["open", "open"]
    store.update("sw", engine="legacy")
    tick("sw", store, ledger, runtime, now_ms=2_000)
    assert [task for _, _, task in runtime.spawned] == ["t1", "t2"]


def test_an_events_engine_swarm_still_reaps_a_worker_the_legacy_path_launched(store):  # noqa: F811
    from scripts.swarm.store import AgentRecord

    store.update("sw", engine="events", state="paused")
    ledger, runtime = tasks(("t1", "eng")), FakeRuntime()
    ledger.rows["t1"].update(state="claimed", claimed_by="engineer@a1b2c3-0009")
    store.put_agent("sw", AgentRecord("engineer@a1b2c3-0009", "eng", "t1", started_at=1_000))
    actions = tick("sw", store, ledger, runtime, now_ms=10_000_000)
    assert [action for action in actions if action.startswith("lost engineer@a1b2c3-0009")]
    assert [a.name for a in store.agents("sw") if a.lane == "eng"] == []


def test_tick_holds_spawning_until_pass_budget_recovers(store: object, monkeypatch: object) -> None:  # noqa: F811
    import json
    from typing import Any, cast
    from unittest.mock import patch

    from scripts.swarm.tick import spawn_holds

    memory = cast(Any, store)
    cast(Any, monkeypatch).setenv("AGENTIHOOKS_TICK_STAGE", "S4")
    ledger, runtime = tasks(("t1", "eng")), FakeRuntime()
    for _ in range(10):
        memory.redis.xadd(memory.key("sw", "tick:passes"), {"record": json.dumps({"wall_s": 101.0})})
    with patch("scripts.inbox.store.InboxStore") as inbox:
        tick("sw", memory, ledger, runtime, now_ms=1_000)
        assert runtime.spawned == []
        assert spawn_holds(memory, "sw") == ["holding spawns: pass p95 101s exceeds S4 target 100s"]
        tick("sw", memory, ledger, runtime, now_ms=2_000)
        assert runtime.spawned == []
        inbox.return_value.send.assert_called_once()
        for _ in range(10):
            memory.redis.xadd(memory.key("sw", "tick:passes"), {"record": json.dumps({"wall_s": 100.0})})
        tick("sw", memory, ledger, runtime, now_ms=3_000)
        assert [task for _, _, task in runtime.spawned] == ["t1"]
        assert spawn_holds(memory, "sw") == []
