import itertools
import json
import threading
from collections import Counter

import pytest

from scripts.inbox import pool
from scripts.inbox import store as inbox_store
from scripts.inbox.pool import POOLS, MasterPool, live_masters, turn
from scripts.inbox.store import RECLAIMED, InboxError, InboxStore, now_ms
from scripts.swarm import operator_mail
from scripts.swarm.store import AgentRecord, RedisStore, SwarmConfig
from scripts.swarm_v2 import masters
from scripts.swarm_v2.masters import MasterSeats

pytestmark = pytest.mark.unit

SLUG = "sw"
FRONT = "master@sw"
SECOND = "master-2@sw"
THIRD = "master-3@sw"
POOL = f"{pool.PREFIX}:sw"


def claimed(seat):
    return f"claimed from the master pool by {seat}"


def _client(server):
    import fakeredis

    return fakeredis.FakeRedis(server=server, decode_responses=True)


@pytest.fixture
def server():
    import fakeredis

    return fakeredis.FakeServer()


@pytest.fixture
def swarm(server):
    store = RedisStore(_client(server))
    store.create(SwarmConfig(SLUG, "/repo", max_eng=1, max_ci=1))
    return store


@pytest.fixture
def inbox(swarm):
    return InboxStore(swarm.redis, session_id="s1")


def _masters(swarm, count, states=None):
    states = states or {}
    for index in range(1, count + 1):
        seat = FRONT if index == 1 else f"master-{index}@{SLUG}"
        name = f"sw-master-{index}"
        swarm.put_agent(SLUG, AgentRecord(name, "master", "master", seat=seat, state=states.get(name, "working")))
        swarm.seats.occupy(seat, name, at=1)


@pytest.fixture
def held(monkeypatch):
    """Items sent meanwhile wait in the pool; undo hands distribution back."""
    monkeypatch.setattr(MasterPool, "distribute", lambda self, slug, live=None: [])
    return monkeypatch


def test_a_single_master_gets_its_items_exactly_as_before(swarm, inbox):
    _masters(swarm, 1)
    item = inbox.send("swarm", FRONT, "check the queue")
    assert swarm.redis.exists(POOL) == 0
    assert swarm.redis.smembers(POOLS) == set()
    assert [i.id for i in inbox.pending_mail("sw-master-1")] == [item.id]
    assert inbox.deliver(item.id, "sw-master-1").state == "delivered"
    assert [inbox.get(i.id).address for i in inbox.redeliver(now_ms() + 10, 0)] == [FRONT]
    assert [(e["state"], e["reason"]) for e in inbox.history(item.id)] == [
        ("pending", ""),
        ("delivered", ""),
        ("pending", "redelivered: never confirmed inside the redelivery window"),
    ]
    assert inbox.get(item.id).address == FRONT
    assert [i.id for i in inbox.pending_mail("sw-master-1")] == [item.id]


def test_items_go_round_robin_across_masters_in_arrival_order(swarm, inbox):
    _masters(swarm, 3)
    items = [inbox.send("swarm", FRONT, f"item {n}") for n in range(6)]
    assert [inbox.get(i.id).address for i in items] == [FRONT, SECOND, THIRD, FRONT, SECOND, THIRD]
    assert [swarm.redis.zrange(inbox.key("pending", seat), 0, -1) for seat in (FRONT, SECOND, THIRD)] == [
        [items[n].id, items[n + 3].id] for n in range(3)
    ]
    assert swarm.redis.smembers(inbox.key("waiting")) == {FRONT, SECOND, THIRD}
    assert [i.id for i in inbox.pending_mail("sw-master-1")] == [items[0].id, items[3].id]
    assert [i.id for i in inbox.pending_mail("sw-master-2")] == [items[1].id, items[4].id]
    assert [i.id for i in inbox.pending_mail("sw-master-3")] == [items[2].id, items[5].id]
    assert inbox.get(items[1].id).reason == claimed(SECOND)
    assert [i.id for i in inbox.open_items(FRONT)] == [items[0].id, items[3].id]
    assert [i.id for i in inbox.open_items(SECOND)] == [items[1].id, items[4].id]
    assert swarm.redis.get(f"{POOL}:last") == THIRD
    assert swarm.redis.exists(POOL) == 0
    assert swarm.redis.smembers(POOLS) == {SLUG}


def test_waiting_items_leave_the_pool_oldest_first(swarm, inbox, held):
    _masters(swarm, 2)
    items = [inbox.send("swarm", FRONT, f"item {n}") for n in range(4)]
    assert swarm.redis.zrange(POOL, 0, -1) == [i.id for i in items]
    assert swarm.redis.smembers(POOLS) == {SLUG}
    held.undo()
    claimed = inbox.pool.distribute(SLUG)
    assert [(i.id, i.address) for i in claimed] == [
        (items[0].id, FRONT),
        (items[1].id, SECOND),
        (items[2].id, FRONT),
        (items[3].id, SECOND),
    ]
    assert inbox.pool.distribute(SLUG) == []
    assert swarm.redis.smembers(POOLS) == {SLUG}


def test_no_master_takes_an_item_still_in_the_pool(swarm, inbox, held):
    _masters(swarm, 2)
    items = [inbox.send("swarm", FRONT, f"who takes {n}") for n in range(3)]
    assert inbox.pending_items(FRONT) == []
    assert inbox.pending_mail("sw-master-1") == []
    assert [inbox.deliver(i.id, "sw-master-1") for i in items] == [None, None, None]
    assert [inbox.get(i.id).state for i in items] == ["pending"] * 3


def test_a_closed_item_drops_out_of_the_pool(swarm, inbox, held):
    _masters(swarm, 2)
    gone, stale, kept = (inbox.send("swarm", FRONT, text) for text in ("gone", "stale", "kept"))
    inbox.withdraw(gone.id, "swarm", "cancelled: no longer needed")
    assert swarm.redis.zrange(POOL, 0, -1) == [stale.id, kept.id]
    swarm.redis.hset(inbox.key("item", stale.id), "state", "done")
    held.undo()
    assert [(i.id, i.address) for i in inbox.pool.distribute(SLUG)] == [(kept.id, FRONT)]
    assert swarm.redis.exists(POOL) == 0


def test_operator_items_bypass_the_pool_to_the_front_door(swarm, inbox):
    _masters(swarm, 2)
    items = [inbox.send("operator", FRONT, f"On ledger sw: line {n}") for n in range(3)]
    assert [inbox.get(i.id).address for i in items] == [FRONT, FRONT, FRONT]
    assert [i.id for i in inbox.pending_mail("sw-master-1")] == [i.id for i in items]
    assert inbox.pending_mail("sw-master-2") == []
    assert swarm.redis.exists(f"{POOL}:last") == 0
    assert all(e["state"] == "pending" and e["reason"] == "" for i in items for e in inbox.history(i.id))


def test_masters_gone_or_draining_are_passed_over(swarm, inbox):
    _masters(swarm, 6, {"sw-master-2": "finished", "sw-master-4": "retiring"})
    swarm.redis.hset(swarm.key(SLUG, "master-retire-requests"), "sw-master-3", json.dumps({"reason": "shrank"}))
    MasterSeats(swarm.redis).retire(SLUG, "master-5@sw", 1)
    assert live_masters(swarm.redis, SLUG) == [FRONT, "master-6@sw"]
    items = [inbox.send("swarm", FRONT, f"item {n}") for n in range(3)]
    assert [inbox.get(i.id).address for i in items] == [FRONT, "master-6@sw", FRONT]
    from scripts.swarm_v2.master_fence import Seats

    fence = Seats(SLUG, swarm, {})
    assert [fence.writer(f"sw-master-{n}") for n in (2, 3, 4)] == ["sw-master-2", "master-3@sw", "master-4@sw"]


@pytest.mark.parametrize("waiting_seat", [FRONT, SECOND])
def test_a_master_waiting_on_input_is_skipped_and_rejoins(swarm, inbox, waiting_seat):
    _masters(swarm, 2)
    name = "sw-master-1" if waiting_seat == FRONT else "sw-master-2"
    other = SECOND if waiting_seat == FRONT else FRONT
    swarm.put_agent(SLUG, AgentRecord(name, "master", "master", seat=waiting_seat, input_prompt="Continue?"))
    items = [inbox.send("swarm", FRONT, f"work {n}") for n in range(2)]
    assert [inbox.get(item.id).address for item in items] == [other, other]
    operator = inbox.send("operator", FRONT, "operator work")
    assert inbox.get(operator.id).address == FRONT
    swarm.put_agent(SLUG, AgentRecord(name, "master", "master", seat=waiting_seat))
    resumed = [inbox.send("swarm", FRONT, f"resumed {n}") for n in range(2)]
    assert [inbox.get(item.id).address for item in resumed] == [waiting_seat, other]


@pytest.mark.parametrize("state", ["pending", "delivered", "confirmed", "read"])
def test_unsettled_pool_mail_leaves_a_master_waiting_on_input(swarm, inbox, state):
    _masters(swarm, 2)
    item = inbox.send("swarm", FRONT, "work")
    if state != "pending":
        inbox.deliver(item.id, "sw-master-1")
    if state == "confirmed":
        inbox.confirm(item.id, "sw-master-1")
    elif state == "read":
        inbox.read(item.id, "sw-master-1")
    operator = inbox.send("operator", FRONT, "operator work")
    swarm.put_agent(SLUG, AgentRecord("sw-master-1", "master", "master", seat=FRONT, input_prompt="Continue?"))
    inbox.pool.distribute_all()
    moved = inbox.get(item.id)
    assert (moved.id, moved.address, moved.state, moved.delivery_session) == (item.id, SECOND, "pending", "")
    assert inbox.get(operator.id).address == FRONT
    assert inbox.history(item.id)[-1]["reason"] == claimed(SECOND)


def test_live_seats_read_lead_first_then_by_seat_number(swarm):
    _masters(swarm, 12)
    seats = [FRONT, *(f"master-{n}@{SLUG}" for n in range(2, 13))]
    assert live_masters(swarm.redis, SLUG) == seats
    assert list(masters.held(swarm.redis, SLUG, swarm.agents(SLUG)).items()) == [
        (seat, f"sw-master-{n}") for n, seat in enumerate(seats, start=1)
    ]


def test_pool_fence_and_front_door_agree_on_the_seats_mid_handoff(swarm, inbox):
    from scripts.swarm.ledger_events import Mail
    from scripts.swarm.operator_mail import master_address
    from scripts.swarm_v2.master_fence import Seats

    _masters(swarm, 2)
    MasterSeats(swarm.redis).set_count(SLUG, 2)
    swarm.put_agent(SLUG, AgentRecord("sw-master-2", "master", "master", seat=SECOND, state="finished"))
    swarm.seats.occupy(SECOND, "sw-master-2b", at=2)
    fence = Seats(SLUG, swarm, {})
    assert live_masters(swarm.redis, SLUG) == fence.live == Mail(inbox, swarm, SLUG).masters == [FRONT, SECOND]
    assert master_address(swarm.redis, SLUG, swarm.agents(SLUG)) == fence.lead == FRONT
    writers = (fence.writer("sw-master-2b"), fence.writer("sw-master-1"), fence.writer("sw-master-2"))
    assert writers == (SECOND, FRONT, "sw-master-2")
    items = [inbox.send("swarm", FRONT, f"item {n}") for n in range(2)]
    assert [inbox.get(i.id).address for i in items] == [FRONT, SECOND]


def test_a_seat_whose_occupant_left_it_is_not_live(swarm):
    _masters(swarm, 3)
    swarm.seats.record_exit("sw-master-3", THIRD, "stopped")
    swarm.put_agent(SLUG, AgentRecord("sw-eng-1", "eng", "t1", seat="eng-1@sw"))
    swarm.seats.occupy("eng-1@sw", "sw-eng-1", at=1)
    assert live_masters(swarm.redis, SLUG) == [FRONT, SECOND]
    swarm.seats.occupy(THIRD, "sw-master-3b", at=2)
    assert live_masters(swarm.redis, SLUG) == [FRONT, SECOND, THIRD]


def test_turn_follows_seat_order_and_passes_over_the_last_holder():
    seats = [FRONT, SECOND, "master-10@sw"]
    assert turn(seats, "", "") == FRONT
    assert turn(seats, FRONT, "") == SECOND
    assert turn(seats, SECOND, "") == "master-10@sw"
    assert turn(seats, "master-10@sw", "") == FRONT
    assert turn(seats, "master-10@sw", FRONT) == SECOND
    assert turn([FRONT, SECOND], SECOND, SECOND) == FRONT
    assert turn([SECOND], SECOND, SECOND) == SECOND
    assert turn(seats, THIRD, "") == "master-10@sw"


def test_only_front_door_items_from_others_are_pooled(swarm, inbox):
    _masters(swarm, 2)
    assert inbox.pool.masters_for("swarm", FRONT) == [FRONT, SECOND]
    assert inbox.pool.masters_for("operator", FRONT) == []
    assert inbox.pool.masters_for("swarm", SECOND) == []
    assert inbox.pool.masters_for("swarm", "eng-1@sw") == []
    assert inbox.pool.masters_for("swarm", "sw-eng-1") == []
    assert pool.slug_of(FRONT) == SLUG
    assert pool.slug_of(SECOND) == ""
    assert pool.slug_of("master@a@b") == "a@b"
    assert (pool.number(FRONT), pool.number("master-2@a@b")) == (1, 2)
    assert (inbox.pool.takes_back(None), inbox.pool.takes_back("")) == (False, False)


def test_an_unconfirmed_item_goes_to_the_next_master_with_the_same_id(swarm, inbox):
    _masters(swarm, 2)
    first, second = (inbox.send("swarm", FRONT, text) for text in ("first", "second"))
    assert (inbox.get(first.id).address, inbox.get(second.id).address) == (FRONT, SECOND)
    assert inbox.deliver(first.id, "sw-master-1").state == "delivered"
    assert [i.id for i in inbox.redeliver(now_ms() + 10, 0)] == [first.id]
    moved = inbox.get(first.id)
    assert (moved.address, moved.state, moved.reason) == (SECOND, "pending", claimed(SECOND))
    assert [i.id for i in inbox.pending_mail("sw-master-2")] == [first.id, second.id]
    assert inbox.pending_mail("sw-master-1") == []
    assert inbox.open_items(FRONT) == []
    assert inbox.deliver(first.id, "sw-master-2").state == "delivered"
    assert [(e["state"], e["by"]) for e in inbox.history(first.id)] == [
        ("pending", "swarm"),
        ("pending", "inbox"),
        ("delivered", "sw-master-1"),
        ("pending", "inbox"),
        ("pending", "inbox"),
        ("delivered", "sw-master-2"),
    ]


def test_an_item_a_backend_master_left_unconfirmed_returns_to_the_front_door(swarm, inbox):
    _masters(swarm, 2)
    first, second = (inbox.send("swarm", FRONT, text) for text in ("first", "second"))
    assert inbox.deliver(first.id, "sw-master-1").state == "delivered"
    assert inbox.deliver(second.id, "sw-master-2").state == "delivered"
    assert inbox.confirm(first.id, "sw-master-1").state == "confirmed"
    assert [i.id for i in inbox.open_items(SECOND)] == [second.id]
    assert [i.address for i in inbox.redeliver(now_ms() + 10, 0)] == [FRONT]
    assert inbox.get(second.id).address == FRONT
    assert inbox.open_items(SECOND) == []
    assert [i.id for i in inbox.pending_mail("sw-master-1")] == [second.id]
    assert int(swarm.redis.get(inbox.key("open-size", SECOND))) == 0
    assert int(swarm.redis.get(inbox.key("open-size", FRONT))) == 2


def test_a_lone_live_master_takes_what_waits(swarm, inbox, held):
    _masters(swarm, 2)
    items = [inbox.send("swarm", FRONT, f"item {n}") for n in range(2)]
    swarm.put_agent(SLUG, AgentRecord("sw-master-2", "master", "master", seat=SECOND, state="finished"))
    held.undo()
    inbox.redeliver(now_ms(), 1000)
    assert [(inbox.get(i.id).address, inbox.get(i.id).reason) for i in items] == [(FRONT, claimed(FRONT))] * 2
    assert swarm.redis.smembers(POOLS) == {SLUG}


def test_items_wait_while_no_master_is_live(swarm, inbox, held):
    _masters(swarm, 2)
    item = inbox.send("swarm", FRONT, "wait")
    for name, seat in (("sw-master-1", FRONT), ("sw-master-2", SECOND)):
        swarm.put_agent(SLUG, AgentRecord(name, "master", "master", seat=seat, state="finished"))
    held.undo()
    assert inbox.pool.distribute(SLUG) == []
    assert swarm.redis.zrange(POOL, 0, -1) == [item.id]


def test_swarm_keys_carry_the_pool(swarm, inbox, held):
    _masters(swarm, 2)
    inbox.send("swarm", FRONT, "wait")
    keys, _ = inbox.keys_for(lambda address: address.endswith("@sw"))
    assert POOL in keys
    held.undo()
    inbox.pool.distribute(SLUG)
    keys, _ = inbox.keys_for(lambda address: address.endswith("@sw"))
    assert POOL not in keys and f"{POOL}:last" in keys and f"{POOL}:holders" in keys


def test_two_concurrent_claimers_take_each_item_exactly_once(swarm, inbox, server, held):
    _masters(swarm, 2)
    items = [inbox.send("swarm", FRONT, f"item {n}") for n in range(40)]
    held.undo()
    claimers = [(InboxStore(_client(server), session_id=f"s{n}"), f"sw-master-{n}") for n in (1, 2)]
    start, errors = threading.Barrier(len(claimers)), []

    def claim(store, name):
        try:
            start.wait()
            for _ in range(20):
                store.pool.distribute(SLUG)
                for item in store.pending_mail(name):
                    store.deliver(item.id, name)
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=claim, args=claimer) for claimer in claimers]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert errors == []
    inbox.pool.distribute(SLUG)
    for store, name in claimers:
        for item in store.pending_mail(name):
            store.deliver(item.id, name)
    holders = {}
    for item in items:
        history = inbox.history(item.id)
        claims = [e for e in history if e["reason"].startswith("claimed from the master pool")]
        delivered = [e["by"] for e in history if e["state"] == "delivered"]
        assert len(claims) == 1 and len(delivered) == 1
        holders[item.id] = inbox.get(item.id).address
        assert delivered == [swarm.seats.occupant(holders[item.id]).occupant]
    assert Counter(holders.values()) == {FRONT: 20, SECOND: 20}
    assert swarm.redis.exists(POOL) == 0


def test_a_master_cannot_read_an_item_still_in_the_pool(swarm, inbox, held):
    _masters(swarm, 2)
    item = inbox.send("swarm", FRONT, "who takes this")
    with pytest.raises(InboxError) as refused:
        inbox.read(item.id, "sw-master-1")
    assert str(refused.value) == f"message {item.id} waits in the master pool for the next master"
    assert inbox.close(item.id, "sw-master-1", "cancel").state == "cancelled"
    assert swarm.redis.exists(POOL) == 0
    held.undo()
    assert inbox.pool.distribute(SLUG) == []
    assert swarm.redis.smembers(POOLS) == set()


def test_an_unconfirmed_item_stays_with_the_last_live_master_as_before(swarm, inbox):
    _masters(swarm, 2)
    item = inbox.send("swarm", FRONT, "first")
    assert inbox.deliver(item.id, "sw-master-1").state == "delivered"
    swarm.put_agent(SLUG, AgentRecord("sw-master-2", "master", "master", seat=SECOND, state="finished"))
    assert [i.id for i in inbox.redeliver(now_ms() + 10, 0)] == [item.id]
    moved = inbox.get(item.id)
    assert (moved.address, moved.state, moved.reason) == (
        FRONT,
        "pending",
        "redelivered: never confirmed inside the redelivery window",
    )
    assert [e["reason"] for e in inbox.history(item.id)][
        -1
    ] == "redelivered: never confirmed inside the redelivery window"
    assert swarm.redis.exists(POOL) == 0


def test_an_item_of_a_gone_holder_returns_to_the_pool_for_the_last_live_master(swarm, inbox):
    _masters(swarm, 2)
    first, second = (inbox.send("swarm", FRONT, text) for text in ("first", "second"))
    assert inbox.deliver(second.id, "sw-master-2").state == "delivered"
    swarm.put_agent(SLUG, AgentRecord("sw-master-2", "master", "master", seat=SECOND, state="finished"))
    inbox.redeliver(now_ms() + 10, 0)
    moved = inbox.get(second.id)
    assert (moved.address, moved.reason) == (FRONT, claimed(FRONT))
    assert inbox.get(first.id).address == FRONT


@pytest.mark.parametrize("state", ["delivered", "confirmed", "read"])
def test_unsettled_pool_mail_of_a_gone_master_returns_with_its_id(swarm, inbox, state):
    _masters(swarm, 2)
    item = inbox.send("swarm", FRONT, "work")
    inbox.deliver(item.id, "sw-master-1")
    if state == "confirmed":
        inbox.confirm(item.id, "sw-master-1")
    elif state == "read":
        inbox.read(item.id, "sw-master-1")
    operator = inbox.send("operator", FRONT, "operator work")
    swarm.put_agent(SLUG, AgentRecord("sw-master-1", "master", "master", seat=FRONT, state="finished"))
    inbox.pool.distribute_all()
    moved = inbox.get(item.id)
    assert (moved.id, moved.address, moved.state) == (item.id, SECOND, "pending")
    assert moved.delivery_session == ""
    assert inbox.get(operator.id).address == FRONT
    assert item.id not in swarm.redis.zrange(inbox.key("delivered"), 0, -1)
    assert [entry["state"] for entry in inbox.history(item.id)][-2:] == ["pending", "pending"]


@pytest.mark.parametrize("exit_text", ["handed off its seat", "retired after the swarm idle limit"])
def test_master_exit_returns_confirmed_pool_mail_but_keeps_operator_mail(swarm, inbox, exit_text):
    from scripts.inbox import exits
    from scripts.swarm.naming import NameRegistry

    _masters(swarm, 2)
    names = NameRegistry(swarm.redis)
    swarm.redis.hset(names.key("name", "sw-master-1"), mapping={"type": "master"})
    item = inbox.send("swarm", FRONT, "work")
    inbox.deliver(item.id, "sw-master-1")
    inbox.confirm(item.id, "sw-master-1")
    operator = inbox.send("operator", FRONT, "operator work")
    inbox.deliver(operator.id, "sw-master-1")
    inbox.confirm(operator.id, "sw-master-1")
    exits.settle(inbox, "sw-master-1", FRONT, exit_text)
    returned = inbox.get(item.id)
    assert (returned.state, returned.reason, returned.delivery_session) == ("pending", RECLAIMED, "")
    inbox.pool.distribute_all()
    assert (inbox.get(item.id).address, inbox.get(item.id).state) == (SECOND, "pending")
    assert (inbox.get(operator.id).address, inbox.get(operator.id).state) == (FRONT, "confirmed")


def test_a_stale_master_exit_leaves_pool_work_taken_by_its_successor(swarm, inbox):
    from scripts.inbox import exits
    from scripts.swarm.naming import NameRegistry

    _masters(swarm, 2)
    names = NameRegistry(swarm.redis)
    swarm.redis.hset(names.key("name", "sw-master-1"), mapping={"type": "master"})
    item = inbox.send("swarm", FRONT, "work")
    inbox.deliver(item.id, "sw-master-1")
    inbox.confirm(item.id, "sw-master-1")
    swarm.seats.occupy(FRONT, "successor", at=2)
    inbox.read(item.id, "successor")
    exits.settle(inbox, "sw-master-1", FRONT, "handed off its seat")
    assert (inbox.get(item.id).address, inbox.get(item.id).state) == (FRONT, "read")


def test_a_stale_master_exit_cannot_return_a_retiring_successors_work(swarm, inbox):
    from scripts.inbox import exits
    from scripts.swarm.naming import NameRegistry

    _masters(swarm, 2)
    names = NameRegistry(swarm.redis)
    swarm.redis.hset(names.key("name", "sw-master-1"), mapping={"type": "master"})
    item = inbox.send("swarm", FRONT, "work")
    inbox.deliver(item.id, "sw-master-1")
    inbox.confirm(item.id, "sw-master-1")
    swarm.seats.occupy(FRONT, "successor", at=2)
    inbox.read(item.id, "successor")
    swarm.put_agent(SLUG, AgentRecord("successor", "master", "master", seat=FRONT, state="retiring"))
    exits.settle(inbox, "sw-master-1", FRONT, "handed off its seat")
    assert (inbox.get(item.id).address, inbox.get(item.id).state) == (FRONT, "read")


def test_a_closed_pool_item_is_not_reopened_when_its_holder_leaves(swarm, inbox):
    _masters(swarm, 2)
    item = inbox.send("swarm", FRONT, "work")
    inbox.close(item.id, "sw-master-1", "done", "answered")
    swarm.put_agent(SLUG, AgentRecord("sw-master-1", "master", "master", seat=FRONT, state="finished"))
    inbox.pool.distribute_all()
    assert (inbox.get(item.id).address, inbox.get(item.id).state) == (FRONT, "done")


def test_a_contended_return_stays_discoverable_for_the_next_pool_pass(swarm, inbox, monkeypatch):
    from redis.exceptions import WatchError

    _masters(swarm, 2)
    item = inbox.send("swarm", FRONT, "work")
    inbox.deliver(item.id, "sw-master-1")
    inbox.confirm(item.id, "sw-master-1")
    swarm.put_agent(SLUG, AgentRecord("sw-master-1", "master", "master", seat=FRONT, state="finished"))
    original = inbox._stage_pool_return
    attempts = []

    def contend_once(*args):
        attempts.append(1)
        if len(attempts) == 1:
            raise WatchError
        return original(*args)

    monkeypatch.setattr(inbox, "_stage_pool_return", contend_once)
    inbox.pool.distribute_all()
    inbox.pool.distribute_all()
    assert (inbox.get(item.id).address, inbox.get(item.id).state) == (SECOND, "pending")


def test_a_successor_receiving_during_reclaim_keeps_its_work(swarm, inbox, monkeypatch):
    _masters(swarm, 2)
    item = inbox.send("swarm", FRONT, "work")
    inbox.deliver(item.id, "sw-master-1")
    inbox.confirm(item.id, "sw-master-1")
    swarm.put_agent(SLUG, AgentRecord("sw-master-1", "master", "master", seat=FRONT, state="finished"))
    original = inbox.return_to_pool

    def successor_takes(item_id, holder):
        swarm.seats.occupy(FRONT, "successor", at=2)
        inbox.read(item.id, "successor")
        return original(item_id, holder)

    monkeypatch.setattr(inbox, "return_to_pool", successor_takes)
    inbox.pool.distribute_all()
    assert (inbox.get(item.id).address, inbox.get(item.id).state) == (FRONT, "read")


def test_returning_settled_or_missing_pool_work_leaves_it_unchanged(swarm, inbox):
    _masters(swarm, 2)
    assert inbox.return_to_pool("missing", FRONT) is None
    item = inbox.send("swarm", FRONT, "work")
    inbox.close(item.id, "sw-master-1", "done", "answered")
    swarm.put_agent(SLUG, AgentRecord("sw-master-1", "master", "master", seat=FRONT, state="finished"))
    assert inbox.return_to_pool(item.id, FRONT) is None
    assert inbox.get(item.id).state == "done"


@pytest.mark.parametrize("receiver", ["sw-master-1", "XXXX"])
def test_pool_return_requires_a_recorded_receipt_for_the_departing_receiver(swarm, inbox, receiver):
    _masters(swarm, 2)
    item = inbox.send("swarm", FRONT, "work")
    assert inbox.return_to_pool(item.id, FRONT, receiver) is None
    assert inbox.get(item.id).state == "pending"


def test_occupying_a_seat_during_pool_return_aborts_the_return(swarm, inbox, monkeypatch):
    _masters(swarm, 2)
    item = inbox.send("swarm", FRONT, "work")
    inbox.deliver(item.id, "sw-master-1")
    inbox.confirm(item.id, "sw-master-1")
    swarm.put_agent(SLUG, AgentRecord("sw-master-1", "master", "master", seat=FRONT, state="finished"))
    original = inbox._stage_pool_return

    def occupy_during_return(*args):
        swarm.seats.occupy(FRONT, "successor", at=2)
        return original(*args)

    monkeypatch.setattr(inbox, "_stage_pool_return", occupy_during_return)
    assert inbox.return_to_pool(item.id, FRONT) is None
    assert inbox.get(item.id).state == "confirmed"


def test_a_contended_return_that_loses_ownership_drops_the_old_holder(swarm, inbox, monkeypatch):
    _masters(swarm, 2)
    item = inbox.send("swarm", FRONT, "work")
    swarm.put_agent(SLUG, AgentRecord("sw-master-1", "master", "master", seat=FRONT, state="finished"))

    def redirect_during_return(item_id, holder):
        inbox.redirect(item_id, "swarm", "eng-1@sw", "assigned to engineer", holder)
        return None

    monkeypatch.setattr(inbox, "return_to_pool", redirect_during_return)
    inbox.pool.distribute_all()
    assert inbox.get(item.id).address == "eng-1@sw"
    assert FRONT not in swarm.redis.smembers(inbox.pool.key(SLUG, "holders"))


def test_a_redirected_item_stays_where_it_was_sent(swarm, inbox):
    _masters(swarm, 2)
    item = inbox.send("swarm", FRONT, "first")
    assert inbox.redirect(item.id, "swarm", "eng-1@sw", "moved to the engineer").address == "eng-1@sw"
    assert swarm.redis.hmget(inbox.key("item", item.id), "pool_holder", "pool_skip") == [None, None]
    swarm.seats.occupy("eng-1@sw", "sw-eng-1", at=1)
    assert inbox.deliver(item.id, "sw-eng-1").state == "delivered"
    inbox.redeliver(now_ms() + 10, 0)
    assert (inbox.get(item.id).address, inbox.get(item.id).state) == ("eng-1@sw", "pending")


def test_a_redirected_item_left_in_the_pool_drops_out_of_it(swarm, inbox, held):
    _masters(swarm, 2)
    item = inbox.send("swarm", FRONT, "first")
    inbox.redirect(item.id, "swarm", "eng-1@sw", "moved to the engineer")
    assert swarm.redis.exists(POOL) == 0
    held.undo()
    assert inbox.pool.distribute(SLUG) == []
    assert inbox.get(item.id).address == "eng-1@sw"


def test_a_redirect_to_the_same_seat_keeps_its_pool_marks(swarm, inbox):
    _masters(swarm, 2)
    inbox.send("swarm", FRONT, "first")
    item = inbox.send("swarm", FRONT, "second")
    assert inbox.redirect(item.id, "swarm", SECOND, "for its next occupant").address == SECOND
    assert swarm.redis.hget(inbox.key("item", item.id), "pool_holder") == SECOND
    assert inbox.deliver(item.id, "sw-master-2").state == "delivered"
    inbox.redeliver(now_ms() + 10, 0)
    assert (inbox.get(item.id).address, inbox.get(item.id).reason) == (FRONT, claimed(FRONT))


def test_an_item_a_gone_master_claimed_but_never_took_goes_to_a_live_master(swarm, inbox, monkeypatch):
    _masters(swarm, 3)
    items = [inbox.send("swarm", FRONT, f"item {n}") for n in range(9)]
    taken = [i.id for i in items[1::3]]
    assert [i.id for i in inbox.pending_items(SECOND)] == taken
    clock = itertools.count(10_000)
    monkeypatch.setattr(inbox_store, "now_ms", lambda: next(clock))
    swarm.put_agent(SLUG, AgentRecord("sw-master-2", "master", "master", seat=SECOND, state="finished"))
    inbox.pool._reclaim(SLUG, [FRONT, THIRD])
    back = [inbox.get(item_id) for item_id in taken]
    assert [(i.address, i.state, i.reason) for i in back] == [(FRONT, "pending", RECLAIMED)] * 3
    assert [swarm.redis.hmget(inbox.key("item", i.id), "pool_holder", "pool_skip") for i in back] == [
        [None, SECOND]
    ] * 3
    assert [inbox.history(i.id)[-1] for i in back] == [
        {"state": "pending", "by": "inbox", "reason": RECLAIMED, "at": i.updated_at} for i in back
    ]
    assert swarm.redis.zrange(POOL, 0, -1) == taken
    assert inbox.pending_items(SECOND) == []
    assert swarm.redis.smembers(f"{POOL}:holders") == {FRONT, THIRD}
    assert inbox.return_to_pool(taken[0], SECOND) is None
    handed = inbox.pool.distribute(SLUG)
    assert [(i.id, i.address) for i in handed] == list(zip(taken, [FRONT, THIRD, FRONT]))
    for before, after in zip(back, (inbox.get(i.id) for i in handed)):
        assert after.updated_at > before.updated_at
        assert inbox.history(after.id)[-1] == {
            "state": "pending",
            "by": "inbox",
            "reason": claimed(after.address),
            "at": after.updated_at,
        }
    assert inbox.pending_mail("sw-master-2") == []


def test_claims_give_up_after_repeated_contention(swarm, inbox, held, monkeypatch):
    from redis.exceptions import WatchError

    _masters(swarm, 2)
    item = inbox.send("swarm", FRONT, "first")
    held.undo()
    calls = []

    def contended(self, slug, live, routed):
        calls.append(slug)
        raise WatchError

    monkeypatch.setattr(MasterPool, "_try_claim", contended)
    assert inbox.pool.distribute(SLUG) == []
    assert calls == [SLUG] * pool.CLAIM_ATTEMPTS
    assert swarm.redis.zrange(POOL, 0, -1) == [item.id]


def test_a_claim_retries_when_the_rotation_moves_meanwhile(swarm, inbox, held, monkeypatch):
    _masters(swarm, 2)
    item = inbox.send("swarm", FRONT, "first")
    held.undo()
    real = pool.turn

    def moved(live, last, skip):
        if not last:
            swarm.redis.set(f"{POOL}:last", FRONT)
        return real(live, last, skip)

    monkeypatch.setattr(pool, "turn", moved)
    assert [(i.id, i.address) for i in inbox.pool.distribute(SLUG)] == [(item.id, SECOND)]


def test_a_claim_retries_when_the_pool_changes_meanwhile(swarm, inbox, held, monkeypatch):
    _masters(swarm, 2)
    first, second = (inbox.send("swarm", FRONT, text) for text in ("first", "second"))
    swarm.redis.zrem(POOL, first.id)
    held.undo()
    real, calls = pool.turn, []

    def changed(live, last, skip):
        if not calls:
            swarm.redis.zadd(POOL, {first.id: first.sequence})
        calls.append(last)
        return real(live, last, skip)

    monkeypatch.setattr(pool, "turn", changed)
    assert [i.id for i in inbox.pool.distribute(SLUG)] == [first.id, second.id]


def test_a_pass_hands_only_the_items_waiting_when_it_began(swarm, inbox, held, monkeypatch):
    _masters(swarm, 2)
    first, second, third = (inbox.send("swarm", FRONT, text) for text in ("first", "second", "third"))
    swarm.redis.zrem(POOL, second.id, third.id)
    held.undo()
    real, calls = pool.turn, []

    def arriving(live, last, skip):
        late = {0: second, 2: third}.get(len(calls))
        if late:
            swarm.redis.zadd(POOL, {late.id: late.sequence})
        calls.append(last)
        return real(live, last, skip)

    monkeypatch.setattr(pool, "turn", arriving)
    assert [i.id for i in inbox.pool.distribute(SLUG)] == [first.id, second.id]
    assert swarm.redis.zrange(POOL, 0, -1) == [third.id]


def test_a_redirect_clears_the_marks_of_an_item_waiting_to_return(swarm, inbox, monkeypatch):
    _masters(swarm, 2)
    inbox.send("swarm", FRONT, "first")
    item = inbox.send("swarm", FRONT, "second")
    assert inbox.deliver(item.id, "sw-master-2").state == "delivered"
    monkeypatch.setattr(MasterPool, "distribute", lambda self, slug: [])
    inbox.redeliver(now_ms() + 10, 0)
    assert swarm.redis.hmget(inbox.key("item", item.id), "pool_holder", "pool_skip") == [None, SECOND]
    assert swarm.redis.zrange(POOL, 0, -1) == [item.id]
    inbox.redirect(item.id, "swarm", "eng-1@sw", "moved to the engineer")
    assert swarm.redis.hmget(inbox.key("item", item.id), "pool_holder", "pool_skip") == [None, None]
    assert swarm.redis.exists(POOL) == 0


def test_operator_ledger_writes_reach_the_front_door_past_the_pool(swarm, inbox):
    from scripts.swarm_ledger.watch_ledger import line

    _masters(swarm, 2)
    events = [
        {
            "rev": 5,
            "at": 5,
            "by": "operator",
            "kind": "message added",
            "target": "chat",
            "id": "c-5",
            "text": "how far",
        },
        {
            "rev": 6,
            "at": 6,
            "by": "operator",
            "kind": "answer added",
            "target": "questions/q1",
            "id": "c-6",
            "text": "yes",
        },
        {"rev": 7, "at": 7, "by": "operator", "kind": "checked", "target": "followups/f1", "id": "c-7"},
    ]
    sent = operator_mail.relay(inbox, swarm, SLUG, {"tasks": []}, events, line)
    assert [(inbox.get(i.id).address, inbox.get(i.id).sender) for i in sent] == [(FRONT, "operator")] * 3
    assert inbox.pending_mail("sw-master-2") == []
    assert swarm.redis.exists(f"{POOL}:last") == 0
