from dataclasses import replace

from scripts.swarm.tick import tick
from tests.swarm.test_tick import HiveAcks, tasks
from tests.swarm.test_tick import store as store  # noqa: F401


def test_an_agent_that_finished_before_its_acknowledgement_was_collected_is_no_spawn_failure(store, monkeypatch):
    from scripts.swarm import failed_panes
    from scripts.swarm import tick as tick_module
    from scripts.swarm.tick import _collect_acks

    ledger, runtime = tasks(("t1", "eng")), HiveAcks()
    tick("sw", store, ledger, runtime, now_ms=1_000)
    name = runtime.ordered[0]
    agent = next(a for a in store.agents("sw") if a.name == name)
    store.put_agent("sw", replace(agent, state="finished"))
    runtime.failing.add(name)
    released = []
    monkeypatch.setattr(tick_module, "_release_hive_launch", lambda slug, store, record: released.append(record.name))
    actions = _collect_acks("sw", store.config("sw"), store, ledger, runtime, {"tasks": []}, 2_000)
    assert actions == [f"{name} finished t1 before the tick collected its hive acknowledgement"]
    assert released == [name]
    assert runtime.collected == []
    assert [(row["agent"], row["state"], row["error"]) for row in store.launches("sw")] == [(name, "started", "")]
    assert failed_panes.held(store, "sw") == {}
    assert [a.state for a in store.agents("sw") if a.name == name] == ["finished"]
