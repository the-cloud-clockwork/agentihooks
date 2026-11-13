from dataclasses import replace

from scripts.inbox.store import InboxStore
from scripts.swarm import preempt
from scripts.swarm.tick import tick
from tests.swarm.test_tick import FakeLedger, FakeRuntime, store, workers  # noqa: F401


def asks(store, name):  # noqa: F811
    return [item for item in InboxStore(store.redis).open_items(name) if item.ref.startswith(f"{preempt.REF}:")]


def busy_with_normal_work(store):  # noqa: F811
    ledger = FakeLedger(
        [{"id": "t0", "state": "done", "done": True}, {"id": "t1", "rank": "high"}, {"id": "t2", "depends_on": ["t0"]}]
    )
    runtime = FakeRuntime()
    tick("sw", store, ledger, runtime, now_ms=1_000)
    tick("sw", store, ledger, runtime, now_ms=2_000)
    ledger.rows["t3"] = {"id": "t3", "lane": "eng", "state": "open", "claimed_by": "", "rank": "urgent"}
    return ledger, runtime


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
    assert "t2" not in {a.task for a in workers(store)}


def test_a_full_lane_probes_placement_again_before_asking_for_an_urgent_task_refused_long_ago(
    store,  # noqa: F811
    monkeypatch,
):
    from scripts.hive import placement_waits

    refused = "no eng hive has room: cpu"
    ledger, runtime = busy_with_normal_work(store)
    ledger.rows["t3"]["rank"] = "high"
    monkeypatch.setattr(
        runtime, "placement_refusal", lambda lane, task: refused if task.get("id") == "t4" else "", raising=False
    )
    ledger.rows["t4"] = {"id": "t4", "lane": "eng", "state": "open", "claimed_by": "", "rank": "urgent"}
    placement_waits.observe(store, "sw", "eng", "t4", refused, 3_000)
    for at in (3_000, 4_000 + placement_waits.WAIT_MS, 5_000 + placement_waits.WAIT_MS + preempt.ANSWER_MS):
        tick("sw", store, ledger, runtime, now_ms=at)
    assert workers(store)
    assert [a.name for a in store.agents("sw") if asks(store, a.name)] == []


def test_an_urgent_task_with_an_unresolved_profile_asks_nobody_once_its_refusal_is_old(
    store,  # noqa: F811
    monkeypatch,
):
    from scripts.hive import placement_waits
    from scripts.swarm.profile_choice import ProfileUnresolved

    def refusal(lane, task):
        if task.get("id") == "t4":
            raise ProfileUnresolved("t4 profile is unresolved")
        return ""

    ledger, runtime = busy_with_normal_work(store)
    ledger.rows["t3"]["rank"] = "high"
    monkeypatch.setattr(runtime, "placement_refusal", refusal, raising=False)
    ledger.rows["t4"] = {"id": "t4", "lane": "eng", "state": "open", "claimed_by": "", "rank": "urgent"}
    placement_waits.observe(store, "sw", "eng", "t4", "no eng hive has room: cpu", 3_000)
    for at in (3_000, 4_000 + placement_waits.WAIT_MS):
        tick("sw", store, ledger, runtime, now_ms=at)
    assert workers(store)
    assert [a.task for a in store.agents("sw") if asks(store, a.name)] == []


def test_spawn_order_shares_one_handoff_batch_across_lanes_and_queue_sort(store, monkeypatch):  # noqa: F811
    from importlib import import_module
    from unittest.mock import Mock, call

    from scripts.swarm import capacity, dispatcher, profile_first
    from tests.swarm.test_preempt import Keyed

    tick_module = import_module("scripts.swarm.tick")
    tasks = [
        {"id": "g", "lane": "eng", "state": "open"},
        {"id": "u", "lane": "ci", "rank": "urgent", "state": "open"},
        {"id": "n", "lane": "eng", "state": "open"},
    ]
    store.put_handoff("sw", "g", "pushed")
    batch = Mock(wraps=store.redis.mget)
    monkeypatch.setattr(store.redis, "mget", batch)
    monkeypatch.setattr(capacity, "read", lambda *args: {})
    monkeypatch.setattr(capacity, "caps", lambda *args: {"eng": 2, "ci": 1})
    monkeypatch.setattr(dispatcher, "claim_key", lambda *args: Keyed())
    monkeypatch.setattr(profile_first, "placeable", lambda slug, config, store, runtime, lane, ready, cap: (ready, []))
    seen = []

    def ready(slug, store, rows, doc, lanes, dispatched, claims=None):
        seen.extend(lanes)
        return {lane: sorted([task for task in tasks if task["lane"] == lane], key=dispatched) for lane in lanes}

    monkeypatch.setattr(tick_module, "ready_by_lane", ready)
    order, waits = tick_module._spawn_order(
        "sw", store.config("sw"), store, [], {task["id"]: task for task in tasks}, {"tasks": tasks}, FakeRuntime()
    )
    assert seen == ["eng", "ci"]
    assert order == [("ci", tasks[1]), ("eng", tasks[0]), ("eng", tasks[2])]
    assert waits == []
    assert batch.call_args_list == [
        call([store.key("sw", "handoff", task["id"]) for task in tasks]),
        call([store.key("sw", "claim", task["id"]) for task in tasks]),
    ]


def test_no_holder_is_asked_while_a_seat_is_free(store):  # noqa: F811
    store.update("sw", max_eng=3)
    ledger, runtime = busy_with_normal_work(store)
    for now in (3_000, 4_000, 5_000):
        tick("sw", store, ledger, runtime, now_ms=now)
    assert sorted(task for _, _, task in runtime.spawned) == ["t1", "t2", "t3"]
    assert all(asks(store, a.name) == [] for a in workers(store))
