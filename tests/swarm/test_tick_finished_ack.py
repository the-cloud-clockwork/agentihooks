from dataclasses import replace

import pytest

from scripts.swarm import failed_panes
from scripts.swarm import tick as tick_module
from scripts.swarm.tick import _acknowledging, _collect_acks, tick
from tests.swarm.test_tick import HiveAcks, tasks
from tests.swarm.test_tick import store as store


def _finished_before_ack(store, monkeypatch, *ids, receipt=True):
    ledger, runtime = tasks(*[(i, "eng") for i in ids or ("t1",)]), HiveAcks()
    tick("sw", store, ledger, runtime, now_ms=1_000)
    for agent in [a for a in store.agents("sw") if a.name in runtime.ordered]:
        store.put_agent("sw", replace(agent, state="finished", harness="claude"))
        if not receipt:
            runtime.failing.add(agent.name)
    released = []
    monkeypatch.setattr(
        tick_module, "_release_hive_launch", lambda s, st, record: released.append((s, st is store, record.name))
    )
    return ledger, runtime, runtime.ordered[0], released


def test_an_agent_that_finished_before_its_acknowledgement_was_collected_is_no_spawn_failure(store, monkeypatch):
    ledger, runtime, name, released = _finished_before_ack(store, monkeypatch)
    store.mark_restart("sw", "t1", "reclaimed")
    lives = store.claims("sw", "t1")
    doc = {"tasks": [{"id": "t1", "lane": "eng", "state": "done"}]}
    actions = _collect_acks("sw", store.config("sw"), store, ledger, runtime, doc, 2_000)
    assert actions == [f"{name} finished t1 before the tick collected its hive acknowledgement"]
    assert released == [("sw", True, name)]
    assert runtime.collected == [name]
    rows = [(r["agent"], r["state"], r["error"], r["after"]) for r in store.launches("sw")]
    assert rows == [(name, "started", "", "reclaimed")]
    assert store.claims("sw", "t1") == lives + 1
    assert store.spawns("sw") == {"claude": 1}
    assert store.take_restart("sw", "t1") == ""
    assert failed_panes.held(store, "sw") == {}
    assert [a.state for a in store.agents("sw") if a.name == name] == ["finished"]
    assert not _acknowledging("sw", store, next(a for a in store.agents("sw") if a.name == name))


def test_a_done_task_the_master_closed_records_the_master_as_why_it_was_open(store, monkeypatch):
    ledger, runtime, name, _ = _finished_before_ack(store, monkeypatch)
    doc = {
        "tasks": [{"id": "t1", "lane": "eng", "state": "done"}],
        "_meta": {"stamps": {"tasks/t1/state": {"by": "master@abc123-0001"}}},
    }
    _collect_acks("sw", store.config("sw"), store, ledger, runtime, doc, 2_000)
    assert [(r["agent"], r["state"], r["after"]) for r in store.launches("sw")] == [(name, "started", "master")]


def test_a_restart_cause_wins_over_the_master_who_closed_the_task(store, monkeypatch):
    ledger, runtime, name, _ = _finished_before_ack(store, monkeypatch)
    store.mark_restart("sw", "t1", "reclaimed")
    doc = {
        "tasks": [{"id": "t1", "lane": "eng", "state": "done"}],
        "_meta": {"stamps": {"tasks/t1/state": {"by": "master@abc123-0001"}}},
    }
    _collect_acks("sw", store.config("sw"), store, ledger, runtime, doc, 2_000)
    assert [(r["agent"], r["state"], r["after"]) for r in store.launches("sw")] == [(name, "started", "reclaimed")]


@pytest.mark.parametrize("task_state", ["claimed", "done"])
def test_an_agent_finished_before_a_failed_acknowledgement_is_still_a_spawn_failure(store, monkeypatch, task_state):
    ledger, runtime, name, released = _finished_before_ack(store, monkeypatch, receipt=False)
    doc = {"tasks": [{"id": "t1", "lane": "eng", "state": task_state}]}
    actions = _collect_acks("sw", store.config("sw"), store, ledger, runtime, doc, 2_000)
    assert len(actions) == 1
    assert actions[0].startswith("spawn failed for t1")
    assert actions[0].endswith(": hive launch failed: prompt")
    assert released == [("sw", True, name)]
    assert [(r["state"], r["error"]) for r in store.launches("sw") if r["agent"] == name] == [
        ("failed", "hive launch failed: prompt")
    ]


def test_every_agent_finished_before_its_acknowledgement_is_collected_in_one_pass(store, monkeypatch):
    ledger, runtime, _, released = _finished_before_ack(store, monkeypatch, "t1", "t2")
    doc = {"tasks": [{"id": t, "lane": "eng", "state": "done"} for t in ("t1", "t2")]}
    actions = _collect_acks("sw", store.config("sw"), store, ledger, runtime, doc, 2_000)
    ordered = {a.name: a.task for a in store.agents("sw") if a.name in runtime.ordered}
    assert len(ordered) == 2
    assert sorted(actions) == sorted(
        f"{name} finished {task} before the tick collected its hive acknowledgement" for name, task in ordered.items()
    )
    assert sorted(r[2] for r in released) == sorted(runtime.ordered)
