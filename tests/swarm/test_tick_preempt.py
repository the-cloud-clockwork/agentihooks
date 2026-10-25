from dataclasses import replace

from scripts.inbox.store import InboxStore
from scripts.swarm.tick import tick
from tests.swarm.test_tick import FakeLedger, FakeRuntime, store, workers  # noqa: F401


def asks(store, name):  # noqa: F811
    return [item for item in InboxStore(store.redis).open_items(name) if item.sender == "swarm"]


def busy_with_normal_work(store):  # noqa: F811
    ledger = FakeLedger(
        [{"id": "t0", "state": "done", "done": True}, {"id": "t1", "rank": "high"}, {"id": "t2", "depends_on": ["t0"]}]
    )
    runtime = FakeRuntime()
    tick("sw", store, ledger, runtime, now_ms=1_000)
    tick("sw", store, ledger, runtime, now_ms=2_000)
    ledger.rows["t3"] = {"id": "t3", "lane": "eng", "state": "open", "claimed_by": "", "rank": "urgent"}
    return ledger, runtime


def test_an_urgent_task_waiting_past_one_tick_with_every_seat_busy_asks_the_lowest_normal_holder_to_hand_off(
    store,  # noqa: F811
):
    ledger, runtime = busy_with_normal_work(store)
    assert sorted(a.task for a in workers(store)) == ["t1", "t2"]
    tick("sw", store, ledger, runtime, now_ms=3_000)
    assert [asks(store, a.name) for a in workers(store)] == [[], []]
    tick("sw", store, ledger, runtime, now_ms=4_000)
    victim = next(a for a in workers(store) if a.task == "t2")
    other = next(a for a in workers(store) if a.task == "t1")
    sent = asks(store, victim.name)
    assert len(sent) == 1 and "t3" in sent[0].text and "handoff" in sent[0].text
    assert asks(store, other.name) == []
    tick("sw", store, ledger, runtime, now_ms=5_000)
    assert len(asks(store, victim.name)) == 1 and asks(store, other.name) == []
    assert "freezes" not in ledger.state("sw")


def test_the_freed_seat_goes_to_the_urgent_task_and_the_normal_successor_yields(store):  # noqa: F811
    ledger, runtime = busy_with_normal_work(store)
    tick("sw", store, ledger, runtime, now_ms=3_000)
    tick("sw", store, ledger, runtime, now_ms=4_000)
    victim = next(a for a in workers(store) if a.task == "t2")
    ledger.rows["t2"]["branch"] = victim.name.replace("@", "-")
    store.put_handoff("sw", "t2", "pushed, preempted for urgent work", seat=victim.seat)
    store.put_agent("sw", replace(victim, state="finished"))
    tick("sw", store, ledger, runtime, now_ms=5_000)
    assert [task for _, _, task in runtime.spawned][2:] == ["t3"]
    assert ledger.rows["t3"]["state"] == "claimed"
    assert (ledger.rows["t2"]["state"], ledger.rows["t2"]["claimed_by"]) == ("open", "")


def test_no_holder_is_asked_while_a_seat_is_free(store):  # noqa: F811
    store.update("sw", max_eng=3)
    ledger, runtime = busy_with_normal_work(store)
    for now in (3_000, 4_000, 5_000):
        tick("sw", store, ledger, runtime, now_ms=now)
    assert sorted(task for _, _, task in runtime.spawned) == ["t1", "t2", "t3"]
    assert all(asks(store, a.name) == [] for a in workers(store))
