import pytest

from scripts.inbox.store import InboxStore
from scripts.swarm import preempt
from scripts.swarm.store import AgentRecord, RedisStore, SwarmConfig

SLUG = "sw"


@pytest.fixture
def store():
    import fakeredis

    s = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    s.create(SwarmConfig(SLUG, "/repo", max_eng=2, max_ci=1))
    return s


def holder(name, task, started_at=0, lane="eng"):
    return AgentRecord(name, lane, task, started_at=started_at)


def asks(store, name):
    return [item for item in InboxStore(store.redis).open_items(name) if item.ref.startswith(f"{preempt.REF}:")]


ROWS = {
    "t1": {"id": "t1", "rank": "high", "state": "claimed"},
    "t2": {"id": "t2", "state": "claimed"},
    "t3": {"id": "t3", "rank": "urgent", "state": "open"},
    "t4": {"id": "t4", "rank": "urgent", "state": "open"},
}
HOLDERS = [holder("e1", "t1"), holder("e2", "t2")]


def doc(*freezes, phases=()):
    return {"tasks": list(ROWS.values()), "phases": list(phases), "freezes": list(freezes)}


def full(*ready, holders=HOLDERS):
    return {"eng": ([ROWS[t] for t in ready], list(holders))}


def test_a_task_seen_waiting_the_first_time_asks_nobody(store):
    assert preempt.step(SLUG, store, ROWS, doc(), full("t3"), 1) == []
    assert asks(store, "e1") == asks(store, "e2") == []


def test_a_task_still_waiting_asks_the_lowest_ranked_holder_once_naming_both_tasks(store):
    preempt.step(SLUG, store, ROWS, doc(), full("t3"), 1)
    actions = preempt.step(SLUG, store, ROWS, doc(), full("t3"), 2)
    sent = asks(store, "e2")
    assert len(sent) == 1 and asks(store, "e1") == []
    assert sent[0].task == "t2" and sent[0].sender == preempt.SENDER
    assert sent[0].text == preempt.TEXT.format(slug=SLUG, waiting="t3", lane="eng", task="t2")
    assert actions == [f"asked e2 to hand off task t2 for urgent task t3 as message {sent[0].id}"]
    assert preempt.step(SLUG, store, ROWS, doc(), full("t3"), 3) == []
    assert len(asks(store, "e2")) == 1 and asks(store, "e1") == []


def test_two_waiting_tasks_ask_two_holders_in_one_step(store):
    preempt.step(SLUG, store, ROWS, doc(), full("t3", "t4"), 1)
    actions = preempt.step(SLUG, store, ROWS, doc(), full("t3", "t4"), 2)
    assert len(actions) == 2
    assert [item.text.split()[6] for item in asks(store, "e2") + asks(store, "e1")] == ["t3", "t4"]


def test_a_task_that_stopped_waiting_starts_its_tick_over(store):
    preempt.step(SLUG, store, ROWS, doc(), full("t3"), 1)
    preempt.step(SLUG, store, ROWS, doc(), full(), 2)
    assert preempt.step(SLUG, store, ROWS, doc(), full("t3"), 3) == []
    assert store.redis.smembers(store.key(SLUG, preempt.WAITING)) == {"t3"}


def test_an_ask_passes_on_only_after_the_answer_window(store):
    preempt.step(SLUG, store, ROWS, doc(), full("t3"), 0)
    preempt.step(SLUG, store, ROWS, doc(), full("t3"), 1)
    assert preempt.step(SLUG, store, ROWS, doc(), full("t3"), preempt.ANSWER_MS) == []
    assert asks(store, "e1") == []
    preempt.step(SLUG, store, ROWS, doc(), full("t3"), 1 + preempt.ANSWER_MS)
    assert len(asks(store, "e1")) == 1 and len(asks(store, "e2")) == 1
    assert store.redis.hgetall(store.key(SLUG, preempt.ASKED)) == {
        "e2": '["t3", 1]',
        "e1": f'["t3", {1 + preempt.ANSWER_MS}]',
    }


def test_an_ask_is_forgotten_once_its_holder_left(store):
    preempt.step(SLUG, store, ROWS, doc(), full("t3"), 0)
    preempt.step(SLUG, store, ROWS, doc(), full("t3"), 1)
    preempt.step(SLUG, store, ROWS, doc(), full("t3", holders=[HOLDERS[0]]), 2)
    assert store.redis.hgetall(store.key(SLUG, preempt.ASKED)) == {"e1": '["t3", 2]'}


def test_an_ask_is_recorded_before_its_message_so_a_failed_tick_never_resends_it(store, monkeypatch):
    preempt.step(SLUG, store, ROWS, doc(), full("t3"), 1)

    class Down(InboxStore):
        def send(self, *args, **kwargs):
            raise ConnectionError("redis went away")

    monkeypatch.setattr(preempt, "InboxStore", Down)
    with pytest.raises(ConnectionError):
        preempt.step(SLUG, store, ROWS, doc(), full("t3"), 2)
    assert store.redis.hgetall(store.key(SLUG, preempt.ASKED)) == {"e2": '["t3", 2]'}
    monkeypatch.setattr(preempt, "InboxStore", InboxStore)
    assert preempt.step(SLUG, store, ROWS, doc(), full("t3"), 3) == []


def test_urgent_focused_and_unknown_holders_are_never_asked(store):
    rows = {**ROWS, "t5": {"id": "t5", "phase": "p1", "state": "claimed"}}
    holders = [holder("e3", "t4"), holder("e4", "t5"), holder("e5", "gone")]
    focused = doc({"verb": "focus", "target": "phases/p1"}, phases=[{"id": "p1"}])
    preempt.step(SLUG, store, rows, focused, full("t3", holders=holders), 1)
    assert preempt.step(SLUG, store, rows, focused, full("t3", holders=holders), 2) == []


def test_a_focused_normal_task_waits_like_an_urgent_one(store):
    rows = {**ROWS, "t6": {"id": "t6", "phase": "p1", "state": "open"}}
    focused = {**doc({"verb": "focus", "target": "phases/p1"}, phases=[{"id": "p1"}]), "tasks": list(rows.values())}
    ready = {"eng": ([rows["t6"]], HOLDERS)}
    preempt.step(SLUG, store, rows, focused, ready, 1)
    assert len(preempt.step(SLUG, store, rows, focused, ready, 2)) == 1
    assert preempt.step(SLUG, store, rows, doc(), {"eng": ([ROWS["t2"]], HOLDERS)}, 3) == []


def test_ties_in_rank_ask_the_holder_started_last(store):
    holders = [holder("e1", "t2", started_at=5), holder("e2", "t7", started_at=9), holder("e3", "t8", started_at=1)]
    rows = {**ROWS, "t7": {"id": "t7"}, "t8": {"id": "t8", "rank": "low"}}
    preempt.step(SLUG, store, rows, doc(), full("t3", holders=holders), 1)
    preempt.step(SLUG, store, rows, doc(), full("t3", holders=holders), 2)
    assert [name for name in ("e1", "e2", "e3") if asks(store, name)] == ["e3"]
    holders[2] = holder("e3", "t9", started_at=1)
    rows["t9"] = {"id": "t9"}
    store.redis.delete(store.key(SLUG, preempt.ASKED))
    preempt.step(SLUG, store, rows, doc(), full("t4", holders=holders), 3)
    preempt.step(SLUG, store, rows, doc(), full("t4", holders=holders), 4)
    assert [item.text.split()[6] for item in asks(store, "e2")] == ["t4"]


class Keyed:
    def __call__(self, task):
        return {"g": (-1, 0), "u": (0, 1), "n": (2, 2)}[task["id"]]


def test_a_successor_yields_just_behind_urgent_work_while_an_urgent_task_is_open(store):
    store.put_handoff(SLUG, "g", "pushed")
    tasks = [{"id": "g", "state": "open"}, {"id": "u", "rank": "urgent", "state": "open"}, {"id": "n", "state": "open"}]
    key = preempt.yielding(store, SLUG, {"tasks": tasks}, Keyed())
    assert [key(t) for t in tasks] == [(preempt.YIELDED, 0), (0, 1), (2, 2)]
    assert sorted(tasks, key=key) == [tasks[1], tasks[0], tasks[2]]


def test_yield_sort_reads_one_batch_and_refreshes_only_for_the_next_pass(store, monkeypatch):
    from unittest.mock import Mock

    tasks = [{"id": "g", "state": "open"}, {"id": "u", "rank": "urgent", "state": "open"}, {"id": "n", "state": "open"}]
    store.put_handoff(SLUG, "g", "pushed")
    batch = Mock(wraps=store.redis.mget)
    monkeypatch.setattr(store.redis, "mget", batch)
    key = preempt.yielding(store, SLUG, {"tasks": tasks}, Keyed())
    batch.assert_called_once_with([store.key(SLUG, "handoff", task["id"]) for task in tasks])
    monkeypatch.setattr(store, "handoff", Mock(side_effect=AssertionError("sort performed a Redis GET")))
    store.clear_handoff(SLUG, "g")
    for _ in range(3):
        assert sorted(tasks, key=key) == [tasks[1], tasks[0], tasks[2]]
    batch.assert_called_once()
    refreshed = preempt.yielding(store, SLUG, {"tasks": tasks}, Keyed())
    assert sorted(tasks, key=refreshed) == tasks
    assert batch.call_count == 2


def test_ordering_is_untouched_without_an_open_urgent_task_or_a_dispatcher_key(store, monkeypatch):
    from unittest.mock import Mock

    monkeypatch.setattr(store.redis, "mget", Mock(side_effect=AssertionError("unneeded Redis batch")))
    store.put_handoff(SLUG, "g", "pushed")
    keyed = Keyed()
    claimed = [{"id": "g", "state": "open"}, {"id": "u", "rank": "urgent", "state": "claimed"}]
    assert preempt.yielding(store, SLUG, {"tasks": claimed}, keyed) is keyed
    assert preempt.yielding(store, SLUG, {"tasks": [{"id": "u", "rank": "urgent", "state": "open"}]}, None) is None


def test_an_urgent_successor_and_a_fresh_guarded_task_keep_their_band(store):
    store.put_handoff(SLUG, "u", "pushed")
    tasks = [{"id": "g", "state": "open"}, {"id": "u", "rank": "urgent", "state": "open"}]
    key = preempt.yielding(store, SLUG, {"tasks": tasks}, Keyed())
    assert [key(t) for t in tasks] == [(-1, 0), (0, 1)]


def test_run_looks_only_at_lanes_whose_every_seat_is_held(store, monkeypatch):
    monkeypatch.setattr(preempt.dispatcher, "claim_key", lambda config, received: lambda task: (2, task["id"]))
    for name, task in (("e1", "t1"), ("e2", "t2"), ("x", "t9")):
        store.put_agent(SLUG, holder(name, task))
    calls = []

    def ready(slug, received, rows, received_doc, lane, dispatched):
        calls.append((slug, received is store, rows is ROWS, received_doc["tasks"], lane, dispatched({"id": "t1"})))
        return [ROWS["t3"]]

    seats = preempt.Seats(ROWS, lambda agent, rows: agent.name == "x", lambda agent, row: True, ready)
    config = store.config(SLUG)
    preempt.run(SLUG, config, store, doc(), 1, seats)
    actions = preempt.run(SLUG, config, store, doc(), 2, seats)
    assert calls == [(SLUG, True, True, doc()["tasks"], "eng", (2, "t1"))] * 2
    assert len(actions) == 1 and len(asks(store, "e2")) == 1
    store.update(SLUG, max_eng=0)
    assert preempt.run(SLUG, store.config(SLUG), store, doc(), 3, seats) == []
    assert len(calls) == 2


def test_a_seat_is_held_by_a_live_agent_that_still_owns_its_task():
    rows = {"t1": {"claimed_by": "e1"}, "t2": {"claimed_by": "someone"}}

    def ended(agent, received):
        return received is rows and agent.name == "x"

    seats = preempt.Seats(rows, ended, lambda agent, row: row.get("claimed_by", agent.name) == agent.name, None)
    agents = [holder("e1", "t1"), holder("x", "t1"), holder("e2", "t2"), holder("e3", "gone")]
    assert [seats.holds(a) for a in agents] == [True, False, False, True]


def test_a_lane_focus_covers_a_normal_task_in_that_lane(store):
    lane_task = {"id": "t6", "lane": "eng", "state": "open"}
    focused = {**doc({"verb": "focus", "target": "lane:eng"}), "tasks": [*ROWS.values(), lane_task]}
    graph = preempt.hierarchy.project(focused)[0]
    assert preempt.preferred(lane_task, focused, graph)
    assert not preempt.preferred({**lane_task, "lane": "ci"}, focused, graph)
