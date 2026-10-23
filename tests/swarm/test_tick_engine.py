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


def test_an_events_engine_swarm_leaves_claimed_tasks_without_an_agent_to_the_hub(store):  # noqa: F811
    store.update("sw", engine="events", state="paused")
    ledger, runtime = tasks(("t1", "eng")), FakeRuntime()
    ledger.rows["t1"].update(state="claimed", claimed_by="engineer@a1b2c3-0009")
    actions = tick("sw", store, ledger, runtime, now_ms=1_000)
    assert (ledger.rows["t1"]["state"], ledger.rows["t1"]["claimed_by"]) == ("claimed", "engineer@a1b2c3-0009")
    assert "task t1 had no agent, reopened" not in actions
    store.update("sw", engine="legacy")
    assert "task t1 had no agent, reopened" in tick("sw", store, ledger, runtime, now_ms=2_000)


def test_an_events_engine_swarm_reaps_no_worker_agent(store):  # noqa: F811
    from scripts.swarm.store import AgentRecord

    store.update("sw", engine="events", state="paused")
    ledger, runtime = tasks(("t1", "eng")), FakeRuntime()
    ledger.rows["t1"].update(state="claimed", claimed_by="engineer@a1b2c3-0009")
    store.put_agent("sw", AgentRecord("engineer@a1b2c3-0009", "eng", "t1", started_at=1_000))
    actions = tick("sw", store, ledger, runtime, now_ms=10_000_000)
    assert [a.name for a in store.agents("sw") if a.lane == "eng"] == ["engineer@a1b2c3-0009"]
    assert not [action for action in actions if action.startswith("lost engineer@a1b2c3-0009")]
    store.update("sw", engine="legacy")
    actions = tick("sw", store, ledger, runtime, now_ms=10_000_001)
    assert [action for action in actions if action.startswith("lost engineer@a1b2c3-0009")]


def test_an_events_engine_swarm_collects_no_worker_hive_acknowledgement(store):  # noqa: F811
    from scripts.swarm.tick import _collect_acks
    from tests.swarm.test_tick import HiveAcks

    ledger, runtime = tasks(("t1", "eng")), HiveAcks()
    tick("sw", store, ledger, runtime, now_ms=1_000)
    assert runtime.ordered
    store.update("sw", engine="events")
    doc = {"tasks": list(ledger.rows.values())}
    assert _collect_acks("sw", store.config("sw"), store, ledger, runtime, doc, 2_000) == []
    assert runtime.collected == []
    store.update("sw", engine="legacy")
    _collect_acks("sw", store.config("sw"), store, ledger, runtime, doc, 3_000)
    assert runtime.collected == runtime.ordered
