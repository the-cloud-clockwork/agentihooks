from dataclasses import replace

from scripts.swarm import failed_panes
from scripts.swarm import tick as tick_module
from scripts.swarm.tick import _acknowledging, _collect_acks, tick
from tests.swarm.test_tick import HiveAcks, tasks
from tests.swarm.test_tick import store as store


def _finished_before_ack(store, monkeypatch, *ids):
    ledger, runtime = tasks(*[(i, "eng") for i in ids or ("t1",)]), HiveAcks()
    tick("sw", store, ledger, runtime, now_ms=1_000)
    for agent in [a for a in store.agents("sw") if a.name in runtime.ordered]:
        store.put_agent("sw", replace(agent, state="finished", harness="claude"))
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
    assert runtime.collected == []
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


def test_an_agent_finished_before_its_acknowledgement_on_an_open_task_is_still_a_spawn_failure(store, monkeypatch):
    ledger, runtime, name, released = _finished_before_ack(store, monkeypatch)
    doc = {"tasks": [{"id": "t1", "lane": "eng", "state": "claimed"}]}
    actions = _collect_acks("sw", store.config("sw"), store, ledger, runtime, doc, 2_000)
    assert len(actions) == 1
    assert actions[0].startswith("spawn failed for t1")
    assert actions[0].endswith(": hive launch failed: prompt")
    assert released == [("sw", True, name)]
    assert [(r["state"], r["error"]) for r in store.launches("sw") if r["agent"] == name] == [
        ("failed", "hive launch failed: prompt")
    ]


def test_an_agent_that_blocked_before_its_live_receipt_was_collected_counts_as_launched(store, monkeypatch):
    ledger, runtime, name, released = _finished_before_ack(store, monkeypatch)
    runtime.live.add(name)
    store.mark_restart("sw", "t1", "reclaimed")
    lives = store.claims("sw", "t1")
    doc = {"tasks": [{"id": "t1", "lane": "eng", "state": "blocked"}]}
    actions = _collect_acks("sw", store.config("sw"), store, ledger, runtime, doc, 2_000)
    assert actions == [f"{name} finished t1 before the tick collected its hive acknowledgement"]
    assert runtime.asked == [name]
    assert runtime.collected == []
    assert released == [("sw", True, name)]
    assert [(r["agent"], r["state"], r["error"], r["after"]) for r in store.launches("sw")] == [
        (name, "started", "", "reclaimed")
    ]
    assert store.claims("sw", "t1") == lives + 1
    assert store.spawns("sw") == {"claude": 1}
    assert [a.state for a in store.agents("sw") if a.name == name] == ["finished"]
    assert failed_panes.held(store, "sw") == {}
    assert _collect_acks("sw", store.config("sw"), store, ledger, runtime, doc, 3_000) == []
    assert store.claims("sw", "t1") == lives + 1
    assert store.spawns("sw") == {"claude": 1}


def test_a_finished_agent_without_a_live_receipt_on_a_blocked_task_is_still_a_spawn_failure(store, monkeypatch):
    ledger, runtime, name, _ = _finished_before_ack(store, monkeypatch)
    doc = {"tasks": [{"id": "t1", "lane": "eng", "state": "blocked"}]}
    actions = _collect_acks("sw", store.config("sw"), store, ledger, runtime, doc, 2_000)
    assert runtime.asked == [name]
    assert len(actions) == 1 and actions[0].startswith("spawn failed for t1")
    assert [(r["state"], r["error"]) for r in store.launches("sw") if r["agent"] == name] == [
        ("failed", "hive launch failed: prompt")
    ]


def test_a_done_task_never_reads_the_receipt(store, monkeypatch):
    ledger, runtime, name, _ = _finished_before_ack(store, monkeypatch)
    doc = {"tasks": [{"id": "t1", "lane": "eng", "state": "done"}]}
    _collect_acks("sw", store.config("sw"), store, ledger, runtime, doc, 2_000)
    assert runtime.asked == []


def test_a_working_agent_never_reads_the_receipt(store, monkeypatch):
    ledger, runtime, name, _ = _finished_before_ack(store, monkeypatch)
    agent = next(a for a in store.agents("sw") if a.name == name)
    store.put_agent("sw", replace(agent, state="launching"))
    runtime.live.add(name)
    doc = {"tasks": [{"id": "t1", "lane": "eng", "state": "blocked"}]}
    _collect_acks("sw", store.config("sw"), store, ledger, runtime, doc, 2_000)
    assert runtime.asked == []
    assert runtime.collected == [name]


def test_a_receipt_read_that_raises_falls_back_to_the_guarded_collect(store, monkeypatch, capsys):
    ledger, runtime, name, _ = _finished_before_ack(store, monkeypatch)

    def broken(config, lane, agent, task):
        raise ConnectionError("redis went away")

    runtime.launched = broken
    doc = {"tasks": [{"id": "t1", "lane": "eng", "state": "blocked"}]}
    actions = _collect_acks("sw", store.config("sw"), store, ledger, runtime, doc, 2_000)
    assert runtime.collected == [name]
    assert len(actions) == 1 and actions[0].startswith("spawn failed for t1")
    assert f"{name} receipt read for t1 raised ConnectionError('redis went away')" in capsys.readouterr().err


def test_an_early_finished_launch_clears_its_saved_assignment(store, monkeypatch):
    ledger, runtime, name, _ = _finished_before_ack(store, monkeypatch, "t1", "t2")
    for task in ("t1", "t2"):
        store.redis.hset(store.key("sw", "launch-assignments"), task, "{}")
    runtime.live.update(runtime.ordered)
    doc = {"tasks": [{"id": "t1", "lane": "eng", "state": "blocked"}, {"id": "t2", "lane": "eng", "state": "done"}]}
    _collect_acks("sw", store.config("sw"), store, ledger, runtime, doc, 2_000)
    assert store.redis.hgetall(store.key("sw", "launch-assignments")) == {}


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
