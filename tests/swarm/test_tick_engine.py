from scripts.swarm.tick import tick
from tests.swarm.test_tick import FakeRuntime, store, tasks  # noqa: F401


def test_an_events_engine_swarm_leaves_task_spawns_to_hive_pulls(store):  # noqa: F811
    store.update("sw", engine="events")
    ledger, runtime = tasks(("t1", "eng"), ("t2", "eng")), FakeRuntime()
    tick("sw", store, ledger, runtime, now_ms=1_000)
    assert [task for _, _, task in runtime.spawned] == []
    assert [row["state"] for row in ledger.rows.values()] == ["open", "open"]


def test_an_events_engine_swarm_still_reaps_a_worker_the_legacy_path_launched(store):  # noqa: F811
    from scripts.swarm.store import AgentRecord

    store.update("sw", engine="events", state="paused")
    ledger, runtime = tasks(("t1", "eng")), FakeRuntime()
    ledger.rows["t1"].update(state="claimed", claimed_by="engineer@a1b2c3-0009")
    store.put_agent("sw", AgentRecord("engineer@a1b2c3-0009", "eng", "t1", started_at=1_000))
    actions = tick("sw", store, ledger, runtime, now_ms=10_000_000)
    assert [action for action in actions if action.startswith("lost engineer@a1b2c3-0009")]
    assert [a.name for a in store.agents("sw") if a.lane == "eng"] == []
