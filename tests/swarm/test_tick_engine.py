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
