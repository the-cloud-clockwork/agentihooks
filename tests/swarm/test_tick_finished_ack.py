from dataclasses import replace

from scripts.swarm import failed_panes
from scripts.swarm import tick as tick_module
from scripts.swarm.tick import _collect_acks, tick
from tests.swarm.test_tick import HiveAcks, tasks
from tests.swarm.test_tick import store as store


def _finished_before_ack(store, monkeypatch):
    ledger, runtime = tasks(("t1", "eng")), HiveAcks()
    tick("sw", store, ledger, runtime, now_ms=1_000)
    name = runtime.ordered[0]
    agent = next(a for a in store.agents("sw") if a.name == name)
    store.put_agent("sw", replace(agent, state="finished"))
    runtime.failing.add(name)
    released = []
    monkeypatch.setattr(tick_module, "_release_hive_launch", lambda slug, store, record: released.append(record.name))
    return ledger, runtime, name, released


def test_an_agent_that_finished_before_its_acknowledgement_was_collected_is_no_spawn_failure(store, monkeypatch):
    ledger, runtime, name, released = _finished_before_ack(store, monkeypatch)
    store.mark_restart("sw", "t1", "reclaimed")
    lives = store.claims("sw", "t1")
    doc = {"tasks": [{"id": "t1", "lane": "eng", "state": "done"}]}
    actions = _collect_acks("sw", store.config("sw"), store, ledger, runtime, doc, 2_000)
    assert actions == [f"{name} finished t1 before the tick collected its hive acknowledgement"]
    assert released == [name]
    assert runtime.collected == []
    rows = [(r["agent"], r["state"], r["error"], r["after"]) for r in store.launches("sw")]
    assert rows == [(name, "started", "", "reclaimed")]
    assert store.claims("sw", "t1") == lives + 1
    assert store.take_restart("sw", "t1") == ""
    assert failed_panes.held(store, "sw") == {}
    assert [a.state for a in store.agents("sw") if a.name == name] == ["finished"]


def test_a_done_task_the_master_closed_records_the_master_as_why_it_was_open(store, monkeypatch):
    ledger, runtime, name, _ = _finished_before_ack(store, monkeypatch)
    doc = {
        "tasks": [{"id": "t1", "lane": "eng", "state": "done"}],
        "_meta": {"stamps": {"tasks/t1/state": {"by": "master@abc123-0001"}}},
    }
    _collect_acks("sw", store.config("sw"), store, ledger, runtime, doc, 2_000)
    assert [(r["agent"], r["state"], r["after"]) for r in store.launches("sw")] == [(name, "started", "master")]


def test_an_agent_finished_before_its_acknowledgement_on_an_open_task_is_still_a_spawn_failure(store, monkeypatch):
    ledger, runtime, name, released = _finished_before_ack(store, monkeypatch)
    doc = {"tasks": [{"id": "t1", "lane": "eng", "state": "claimed"}]}
    actions = _collect_acks("sw", store.config("sw"), store, ledger, runtime, doc, 2_000)
    assert [a for a in actions if a.startswith("spawn failed for t1")]
    assert released == [name]
    assert [r["state"] for r in store.launches("sw") if r["agent"] == name] != ["started"]
