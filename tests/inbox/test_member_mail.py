import json
from dataclasses import replace

import pytest

from scripts.hive import reports
from scripts.inbox import exits, wake
from scripts.inbox.store import InboxError, InboxStore
from scripts.swarm import idle
from scripts.swarm.store import APP_SERVER_TRANSPORT, MASTER, AgentRecord, RedisStore, SwarmConfig
from tests.inbox.test_wake import FakeHerdr, FakeLedger

pytestmark = pytest.mark.unit

SEAT = "eng-1@sw"
MASTER_NAME = "sw-master-1"


@pytest.fixture
def redis():
    import fakeredis

    return fakeredis.FakeRedis(server=fakeredis.FakeServer(), decode_responses=True)


@pytest.fixture
def store(redis):
    from scripts.hive import auth

    store = RedisStore(redis)
    store.create(SwarmConfig("sw", "agentihooks", 1, 0))
    redis.hset(f"{auth.PREFIX}:member:m1", mapping={"name": "m1", "ledger": "digest"})
    redis.sadd(auth.INDEX, "m1")
    return store


@pytest.fixture
def agent(store):
    name = store.next_name("sw", "eng")
    store.start_execution("sw", AgentRecord(name, "eng", "t1", seat=SEAT, hive="m1"))
    store.seats.occupy(SEAT, name, 1)
    return next(a for a in store.agents("sw") if a.name == name)


@pytest.fixture
def mail(redis, agent):
    item = InboxStore(redis).send("sender", agent.name, "contract")
    member = InboxStore(redis, world="m1")
    assert InboxStore(redis, world="").inbox(agent.name) == []
    assert [i.id for i in member.inbox(agent.name)] == [item.id]
    return member, item


def test_the_controller_inbox_reads_every_member_world_with_its_persist_hook(store):
    persist = object()
    assert [(w.world, w.persist) for w in InboxStore(store.redis, persist=persist, world="").worlds()] == [
        ("", persist),
        ("m1", persist),
    ]


def test_a_member_world_inbox_reads_only_itself_on_the_controller(store):
    assert [w.world for w in InboxStore(store.redis, world="m1").worlds()] == ["m1"]


def test_the_wake_pass_escalates_a_member_agents_unread_mail_to_the_controller_master(redis, agent, mail):
    member, item = mail
    agents = [agent, AgentRecord(MASTER_NAME, MASTER, MASTER, pane_id="pm")]
    actions = wake.wake_pass(InboxStore(redis), "sw", agents, FakeHerdr({}), FakeLedger(), item.created_at + 10, 1)
    assert f"raised message {item.id} to {MASTER_NAME}" in actions
    assert wake.TO_MASTER in [e.get("event") for e in member.history(item.id)]
    [raised] = InboxStore(redis, world="").pending_items(MASTER_NAME)
    assert raised.ref == f"inbox-escalation:{item.id}"


def hive_wakes(redis, hive="m1"):
    from scripts.hive.orders import CommandOrder, Orders

    entries = redis.xrange(Orders(redis).commands(hive))
    return [CommandOrder(**json.loads(fields["order"])) for _, fields in entries]


def remote_codex(redis, agent, transport=""):
    redis.set(reports.key("m1"), json.dumps({}))
    return replace(agent, harness="codex", transport=transport, pane_id="p1", runtime_target={"host_id": "m1"})


def test_the_wake_pass_orders_the_hive_to_wake_its_idle_legacy_codex_pane(redis, agent, mail):
    member, item = mail
    remote = remote_codex(redis, agent)
    idle.beat(redis, "sw", remote.name, idle.IDLE, item.created_at)
    herdr = FakeHerdr({"p1": "idle"})
    wake.wake_pass(InboxStore(redis), "sw", [remote], herdr, FakeLedger(), item.created_at + 1, 300_000)
    assert herdr.prompts == []
    [order] = hive_wakes(redis)
    assert (order.action, order.slug, order.name, order.text) == ("nudge", "sw", remote.name, wake.WAKE_TEXT)
    assert (order.execution_id, order.generation) == (remote.execution_id, remote.generation)
    assert order.id.startswith(f"wake:{remote.execution_id}:{remote.generation}:")
    assert wake.WOKEN in [e.get("event") for e in member.history(item.id)]


def test_the_waker_orders_the_hive_the_moment_mail_lands_for_its_idle_legacy_codex_pane(redis, agent, mail):
    _, item = mail
    remote = remote_codex(redis, agent)
    idle.beat(redis, "sw", remote.name, idle.IDLE, item.created_at)
    wake.wake_now(InboxStore(redis), "sw", [remote], FakeHerdr({}), item.created_at + 1, 300_000)
    assert [order.action for order in hive_wakes(redis)] == ["nudge"]


def test_a_working_hive_codex_pane_gets_no_wake_and_escalates_as_before(redis, agent, mail):
    _, item = mail
    remote = remote_codex(redis, agent)
    idle.beat(redis, "sw", remote.name, idle.WORKING, item.created_at)
    agents = [remote, AgentRecord(MASTER_NAME, MASTER, MASTER, pane_id="pm")]
    actions = wake.wake_pass(InboxStore(redis), "sw", agents, FakeHerdr({}), FakeLedger(), item.created_at + 10, 1)
    assert hive_wakes(redis) == []
    assert f"raised message {item.id} to {MASTER_NAME}" in actions


def test_a_hive_codex_pane_with_no_heartbeat_gets_no_wake(redis, agent, mail):
    _, item = mail
    wake.wake_now(InboxStore(redis), "sw", [remote_codex(redis, agent)], FakeHerdr({}), item.created_at + 1, 300_000)
    assert hive_wakes(redis) == []


def test_a_hive_codex_pane_whose_working_beat_went_stale_gets_its_wake(redis, agent, mail):
    _, item = mail
    remote = remote_codex(redis, agent)
    now = item.created_at + 1
    idle.beat(redis, "sw", remote.name, idle.WORKING, now - idle.STALE_MS)
    wake.wake_now(InboxStore(redis), "sw", [remote], FakeHerdr({}), now, 300_000)
    assert [order.action for order in hive_wakes(redis)] == ["nudge"]


def test_a_hive_that_stopped_reporting_gets_no_wake_order_and_the_wake_stays_unspent(redis, agent, mail):
    member, item = mail
    remote = remote_codex(redis, agent)
    redis.delete(reports.key("m1"))
    idle.beat(redis, "sw", remote.name, idle.IDLE, item.created_at)
    wake.wake_pass(InboxStore(redis), "sw", [remote], FakeHerdr({}), FakeLedger(), item.created_at + 1, 300_000)
    assert hive_wakes(redis) == []
    assert wake.WOKEN not in [e.get("event") for e in member.history(item.id)]


def test_a_hive_codex_pane_with_a_push_pump_gets_no_typed_wake(redis, agent, mail):
    member, item = mail
    remote = remote_codex(redis, agent, APP_SERVER_TRANSPORT)
    idle.beat(redis, "sw", remote.name, idle.IDLE, item.created_at)
    wake.wake_pass(InboxStore(redis), "sw", [remote], FakeHerdr({}), FakeLedger(), item.created_at + 1, 300_000)
    assert hive_wakes(redis) == []
    assert wake.WOKEN not in [e.get("event") for e in member.history(item.id)]


def test_the_exit_sweep_moves_a_gone_member_agents_mail_to_its_seat(store, agent, mail):
    member, item = mail
    store.put_agent("sw", replace(agent, state="finished"))
    exits.sweep(InboxStore(store.redis), "sw", store, lambda: {})
    assert member.get(item.id).address == SEAT


class AnsweringPingThenHung:
    """A hive Redis that answers ping, then times out on every later call."""

    def ping(self):
        return True

    def __getattr__(self, name):
        from redis.exceptions import TimeoutError as RedisTimeoutError

        def hung(*args, **kwargs):
            raise RedisTimeoutError("Timeout reading from socket")

        return hung


@pytest.fixture
def silent_redis():
    """A real Redis client on a socket the kernel accepts and nobody ever answers, with a short socket timeout."""
    import socket

    import redis as redis_lib

    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(8)
    host, port = listener.getsockname()
    client = redis_lib.Redis(host=host, port=port, socket_timeout=0.2, socket_connect_timeout=0.2)
    yield client
    client.close()
    listener.close()


def _with_world(monkeypatch, world):
    worlds = InboxStore.worlds
    monkeypatch.setattr(InboxStore, "worlds", lambda self: [*worlds(self), world])


@pytest.mark.wall_clock
def test_the_exit_sweep_finishes_past_a_world_that_never_answers_and_settles_the_others(
    store, agent, mail, monkeypatch, silent_redis, capsys
):
    import time

    member, item = mail
    _with_world(monkeypatch, InboxStore(silent_redis, world="m2"))
    store.put_agent("sw", replace(agent, state="finished"))
    started = time.monotonic()
    lines = exits.sweep(InboxStore(store.redis), "sw", store, lambda: {})
    assert time.monotonic() - started < 20
    assert member.get(item.id).address == SEAT
    [line] = lines
    assert line.startswith("skipped the inbox of hive m2 this pass: its Redis timed out or dropped: Timeout ")
    assert capsys.readouterr().err == f"{line}\n"


def test_the_exit_sweep_skips_a_world_that_stops_answering_after_ping_and_settles_the_others(
    store, agent, mail, monkeypatch, capsys
):
    member, item = mail
    _with_world(monkeypatch, InboxStore(AnsweringPingThenHung(), world="m2"))
    store.put_agent("sw", replace(agent, state="finished"))
    lines = exits.sweep(InboxStore(store.redis), "sw", store, lambda: {})
    assert member.get(item.id).address == SEAT
    assert lines == [
        "skipped the inbox of hive m2 this pass: its Redis timed out or dropped: Timeout reading from socket"
    ]
    assert capsys.readouterr() == ("", f"{lines[0]}\n")


def test_a_skipped_controller_world_is_named_the_controller_and_leaves_the_pass(capsys):
    world = InboxStore(AnsweringPingThenHung(), world="")
    worlds = exits._WorldPass([world])
    assert worlds.run(world, lambda w: w.redis.get("key")) is None
    assert worlds.up == []
    assert worlds.skipped == [
        "skipped the inbox of the controller this pass: its Redis timed out or dropped: Timeout reading from socket"
    ]
    assert capsys.readouterr() == ("", f"{worlds.skipped[0]}\n")


def test_a_quiet_move_candidate_does_not_stop_the_world_moving_a_later_seat_with_mail():
    from types import SimpleNamespace

    opened, moved = [], []

    class World:
        def quiet(self, seats):
            return {"eng-1@sw"}

        def open_items(self, seat):
            opened.append(seat)
            return [SimpleNamespace(id="i1")]

        def move_world(self, item_id, home, by, reason):
            moved.append((item_id, reason))

    home = SimpleNamespace(world="m2")
    candidates = [
        (SimpleNamespace(seat="eng-1@sw", name="a1"), home),
        (SimpleNamespace(seat="eng-2@sw", name="a2"), home),
    ]
    exits._move_world_mail(World(), candidates)
    assert opened == ["eng-2@sw"]
    assert moved == [("i1", "moved to hive m2, where a2 runs eng-2@sw")]


def test_a_pass_with_every_world_down_records_the_exit_and_the_next_pass_settles_its_mail(
    store, agent, mail, monkeypatch, silent_redis
):
    member, item = mail
    worlds = InboxStore.worlds
    with monkeypatch.context() as patch:
        patch.setattr(InboxStore, "worlds", lambda self: [InboxStore(silent_redis, world="m1")])
        store.put_agent("sw", replace(agent, state="finished"))
        lines = exits.sweep(InboxStore(store.redis), "sw", store, lambda: {})
    assert [line.split(":")[0] for line in lines] == ["skipped the inbox of hive m1 this pass"]
    assert store.seats.exits([agent.name])[agent.name]
    assert member.get(item.id).address == agent.name
    assert InboxStore.worlds is worlds
    assert exits.sweep(InboxStore(store.redis), "sw", store, lambda: {}) == []
    assert member.get(item.id).address == SEAT


def test_the_tick_redis_client_every_world_reads_through_carries_a_socket_timeout(monkeypatch):
    import redis as redis_lib

    from scripts.swarm import store as swarm_store

    seen = {}

    class Client:
        def ping(self):
            return True

    monkeypatch.setattr(redis_lib.Redis, "from_url", lambda url, **kwargs: seen.update(kwargs) or Client())
    swarm_store._pinged(redis_lib, "redis://hive:6379")
    assert seen["socket_timeout"] == 10
    assert seen["socket_connect_timeout"] == 3


def test_the_exit_sweep_withdraws_the_master_escalation_of_a_member_agents_unfillable_seat(store, agent, mail):
    member, item = mail
    agents = [agent, AgentRecord(MASTER_NAME, MASTER, MASTER, pane_id="pm")]
    wake.wake_pass(InboxStore(store.redis), "sw", agents, FakeHerdr({}), FakeLedger(), item.created_at + 10, 1)
    [raised] = InboxStore(store.redis, world="").pending_items(MASTER_NAME)
    store.put_agent("sw", replace(agent, state="finished"))
    store.update("sw", state="stopped")
    exits.sweep(InboxStore(store.redis), "sw", store, lambda: {})
    assert member.get(item.id).state == "cancelled"
    assert InboxStore(store.redis, world="").get(raised.id).state == "cancelled"


class Live:
    def __init__(self, *names):
        self.names = set(names)

    def live_names(self):
        return self.names


def test_swarm_status_lists_a_member_agents_pending_mail(store, agent, mail, monkeypatch):
    from scripts.swarm import status

    monkeypatch.setattr(status, "page_quota", lambda: {})
    report = status.status_report(store, "sw", {"tasks": []}, Live(agent.name))
    [row] = [a for a in report["agents"] if a["name"] == agent.name]
    assert [m["text"] for m in row["inbox"]] == ["contract"]


def test_a_handoff_moves_a_member_agents_mail_to_its_seat(redis, agent, mail):
    member, item = mail
    exits.settle(InboxStore(redis), agent.name, SEAT, "handed off its seat")
    assert member.get(item.id).address == SEAT


def test_the_handoff_envelope_lists_a_member_agents_open_mail(store, agent, mail):
    from scripts.handoff import envelope

    _, item = mail
    assert [i["id"] for i in envelope._open_items(store, agent)] == [item.id]


def test_a_handoff_closes_a_member_agents_wait_ended_notice(redis, agent):
    from scripts.swarm import waits

    notice = InboxStore(redis).send(waits.SENDER, SEAT, f"Your wait on checks has ended.{waits._pick_up('t1')} go on")
    member = InboxStore(redis, world="m1")
    assert member.get(notice.id).state == "pending"
    waits.settle_notices(InboxStore(redis), agent, "a handoff")
    assert member.get(notice.id).state == "done"


def test_removing_a_swarm_withdraws_its_member_agents_open_mail(redis, agent, mail):
    member, item = mail
    exits.close_swarm(InboxStore(redis), "sw")
    assert member.get(item.id).state == "cancelled"


def seat_successor(store, previous, hive):
    if hive:
        from scripts.hive import auth

        store.redis.hset(f"{auth.PREFIX}:member:{hive}", mapping={"name": hive, "ledger": "digest"})
        store.redis.sadd(auth.INDEX, hive)
    name = store.next_name("sw", "eng")
    store.start_execution("sw", AgentRecord(name, "eng", "t1", seat=SEAT, hive=hive), previous.execution_id)
    store.seats.occupy(SEAT, name, 2)
    return name


@pytest.mark.parametrize("hive", ["", "m2"])
def test_a_leaving_member_agents_unread_mail_reaches_its_seats_next_occupant_on_another_host(store, agent, mail, hive):
    member, item = mail
    neighbour = store.next_name("sw", "eng")
    store.start_execution("sw", AgentRecord(neighbour, "eng", "t0", seat="eng-0@sw", hive="m1"))
    store.seats.occupy("eng-0@sw", neighbour, 1)
    exits.settle(InboxStore(store.redis), agent.name, SEAT, "handed off its seat")
    store.put_agent("sw", replace(agent, state="finished"))
    successor = seat_successor(store, agent, hive)
    exits.sweep(InboxStore(store.redis), "sw", store, lambda: {})
    target = InboxStore(store.redis, world=hive)
    assert [i.id for i in target.pending_mail(successor)] == [item.id]
    assert member.inbox(SEAT) == []
    moved = target.history(item.id)[-1]
    where = f"hive {hive}" if hive else "the controller"
    assert (moved["by"], moved["reason"]) == ("swarm", f"moved to {where}, where {successor} runs {SEAT}")


def test_seat_mail_a_leaving_member_agent_took_reaches_its_seats_next_occupant_on_the_workstation(store, agent):
    item = InboxStore(store.redis).send("sender", SEAT, "contract")
    member = InboxStore(store.redis, world="m1")
    member.deliver(item.id, agent.name)
    exits.settle(InboxStore(store.redis), agent.name, SEAT, "handed off its seat")
    store.put_agent("sw", replace(agent, state="finished"))
    successor = seat_successor(store, agent, "")
    exits.sweep(InboxStore(store.redis), "sw", store, lambda: {})
    assert [i.id for i in InboxStore(store.redis, world="").pending_mail(successor)] == [item.id]
    assert member.inbox(SEAT) == []


@pytest.mark.parametrize("hive", ["", "m2"])
def test_a_redirect_moves_a_member_item_with_its_history_into_the_inbox_its_address_resolves_to(
    store, agent, mail, hive
):
    member, item = mail
    successor = seat_successor(store, agent, hive)
    moved = member.redirect(item.id, "swarm", successor, "passed on")
    target = InboxStore(store.redis, world=hive)
    assert moved.address == successor
    assert member.inbox(agent.name) == []
    assert [i.id for i in target.pending_items(successor)] == [item.id]
    history = target.history(item.id)
    assert [e["state"] for e in history] == ["pending", "pending"]
    assert (history[-1]["by"], history[-1]["reason"]) == ("swarm", "passed on")


def test_the_inbox_an_address_resolves_to_keeps_the_persist_hook(store, agent):
    persist = object()
    successor = seat_successor(store, agent, "")
    resolved = InboxStore(store.redis, persist=persist, world="m1").home_store(successor)
    assert (resolved.world, resolved.persist) == ("", persist)


def test_a_relocated_item_leaves_its_old_inbox_and_is_fully_indexed_in_its_new_one(store, agent, mail):
    _, first = mail
    redis, persisted = store.redis, []
    member = InboxStore(redis, persist=persisted.append, world="m1")
    second, third = (InboxStore(redis).send("sender", agent.name, text) for text in ("second", "third"))
    member.deliver(second.id, agent.name)
    member.read(second.id, agent.name)
    member.deliver(third.id, agent.name)
    member.about(first.id, "tasks/t1")
    successor = seat_successor(store, agent, "")
    controller = InboxStore(redis, world="")
    earlier = controller.send("sender", successor, "earlier")
    controller.deliver(earlier.id, successor)
    assert not redis.sismember(controller.key("waiting"), successor)
    moved = [member.redirect(item.id, "swarm", successor, "passed on") for item in (first, second, third)]
    assert not any(redis.exists(member.key(kind, first.id)) for kind in ("item", "history", "entry"))
    assert [redis.zcard(member.key(kind, agent.name)) for kind in ("address", "open", "pending")] == [0, 0, 0]
    assert redis.get(member.key("open-size", agent.name)) == "0"
    assert not redis.sismember(member.key("waiting"), agent.name)
    assert redis.zscore(member.key("delivered"), third.id) is None
    relocated = sorted(item.id for item in (first, second, third))
    assert [sorted(redis.zrange(controller.key(kind, successor), 0, -1)) for kind in ("address", "open")] == [
        sorted([earlier.id, *relocated])
    ] * 2
    assert sorted(redis.zrange(controller.key("pending", successor), 0, -1)) == relocated
    assert redis.get(controller.key("open-size", successor)) == "4"
    assert redis.sismember(controller.key("waiting"), successor)
    assert controller.entry_of(first.id) == "tasks/t1"
    assert [controller.get(i.id).sequence for i in (first, second, third)] == [2, 3, 4]
    history = controller.history(second.id)
    assert [e["state"] for e in history] == ["pending", "delivered", "read", "pending"]
    assert history[-1]["at"] == moved[1].updated_at
    assert len(persisted) == 3 and None not in persisted


def test_a_relocated_front_door_item_leaves_its_old_master_pool(store, agent):
    store.seats.occupy("master@sw", agent.name, 1)
    item = InboxStore(store.redis).send("sender", "master@sw", "front door")
    member = InboxStore(store.redis, world="m1")
    store.redis.zadd(member.pool.key("sw"), {item.id: 1})
    store.seats.occupy("master@sw", store.next_name("sw", "eng"), 2)
    member.redirect(item.id, "swarm", "master@sw", "moved")
    assert store.redis.zscore(member.pool.key("sw"), item.id) is None
    assert InboxStore(store.redis, world="").get(item.id).address == "master@sw"


@pytest.fixture
def moved_seat(store, agent, mail):
    from scripts.hive import auth

    store.redis.hset(f"{auth.PREFIX}:member:m2", mapping={"name": "m2", "ledger": "digest"})
    store.redis.sadd(auth.INDEX, "m2")
    member, item = mail
    store.put_agent("sw", replace(agent, state="finished"))
    exits.sweep(InboxStore(store.redis), "sw", store, lambda: {})
    name = store.next_name("sw", "eng")
    store.start_execution("sw", AgentRecord(name, "eng", "t1", seat=SEAT, hive="m2"), agent.execution_id)
    store.seats.occupy(SEAT, name, 2)
    return member, item, name


def test_the_sweep_moves_open_seat_mail_to_the_new_occupants_host(store, moved_seat):
    member, item, name = moved_seat
    exits.sweep(InboxStore(store.redis), "sw", store, lambda: {})
    assert [i.id for i in InboxStore(store.redis, world="m2").open_mail(name)] == [item.id]
    assert member.inbox(SEAT) == []


def test_a_moved_seat_item_is_pending_with_its_history_and_a_move_entry(store, moved_seat):
    member, item, name = moved_seat
    before = member.history(item.id)
    exits.sweep(InboxStore(store.redis), "sw", store, lambda: {})
    target = InboxStore(store.redis, world="m2")
    assert (target.get(item.id).state, target.get(item.id).address) == ("pending", SEAT)
    assert [i.id for i in target.pending_mail(name)] == [item.id]
    history = target.history(item.id)
    assert history[:-1] == before
    assert {k: history[-1][k] for k in ("state", "by", "reason")} == {
        "state": "pending",
        "by": exits.BY,
        "reason": f"moved to hive m2, where {name} runs {SEAT}",
    }


def test_the_sweep_skips_quiet_move_candidates_but_still_moves_a_seat_with_mail(store, agent, mail, monkeypatch):
    from scripts.hive import auth

    store.redis.hset(f"{auth.PREFIX}:member:m2", mapping={"name": "m2", "ledger": "digest"})
    store.redis.sadd(auth.INDEX, "m2")
    member, item = mail
    store.put_agent("sw", replace(agent, state="finished"))
    exits.sweep(InboxStore(store.redis), "sw", store, lambda: {})
    name = store.next_name("sw", "eng")
    store.start_execution("sw", AgentRecord(name, "eng", "t1", seat=SEAT, hive="m2"), agent.execution_id)
    store.seats.occupy(SEAT, name, 2)
    quiet_seats = [f"eng-{n}@sw" for n in range(2, 22)]
    for n, seat in enumerate(quiet_seats, start=2):
        quiet_name = store.next_name("sw", "eng")
        store.start_execution("sw", AgentRecord(quiet_name, "eng", f"t{n}", seat=seat, hive="m1"))
        store.seats.occupy(seat, quiet_name, n)
    calls, original = [], InboxStore.open_items

    def spy(self, address):
        calls.append((self.world, address))
        return original(self, address)

    monkeypatch.setattr(InboxStore, "open_items", spy)
    exits.sweep(InboxStore(store.redis), "sw", store, lambda: {})
    assert not any(address in quiet_seats for _, address in calls)
    assert any(address == SEAT for _, address in calls)
    target = InboxStore(store.redis, world="m2")
    assert [i.id for i in target.open_mail(name)] == [item.id]
    assert member.inbox(SEAT) == []


def test_the_sweep_moves_open_seat_mail_to_a_new_occupant_on_the_controller(store, agent, mail):
    member, item = mail
    store.put_agent("sw", replace(agent, state="finished"))
    exits.sweep(InboxStore(store.redis), "sw", store, lambda: {})
    name = store.next_name("sw", "eng")
    store.start_execution("sw", AgentRecord(name, "eng", "t1", seat=SEAT), agent.execution_id)
    store.seats.occupy(SEAT, name, 2)
    exits.sweep(InboxStore(store.redis), "sw", store, lambda: {})
    assert [i.id for i in InboxStore(store.redis, world="").open_mail(name)] == [item.id]
    assert member.inbox(SEAT) == []
    assert InboxStore(store.redis, world="").get(item.id).reason == f"moved to the controller, where {name} runs {SEAT}"


def test_the_sweep_leaves_seat_mail_in_the_world_its_occupant_runs_on(store, agent, mail):
    member, item = mail
    store.put_agent("sw", replace(agent, state="finished"))
    exits.sweep(InboxStore(store.redis), "sw", store, lambda: {})
    name = store.next_name("sw", "eng")
    store.start_execution("sw", AgentRecord(name, "eng", "t1", seat=SEAT, hive="m1"), agent.execution_id)
    store.seats.occupy(SEAT, name, 2)
    history = member.history(item.id)
    exits.sweep(InboxStore(store.redis), "sw", store, lambda: {})
    assert member.history(item.id) == history
    assert [i.id for i in member.open_mail(name)] == [item.id]


def test_moving_an_item_between_worlds_leaves_a_closed_item_in_place(store, agent, mail):
    member, item = mail
    member.close(item.id, agent.name, "cancel")
    target = InboxStore(store.redis, world="m2")
    assert member.move_world(item.id, target, exits.BY, "moved") is None
    assert member.get(item.id).state == "cancelled"
    assert target.inbox(agent.name) == []


def test_moving_an_item_between_worlds_moves_its_indexes_and_ledger_entry(store, agent, mail, monkeypatch):
    from scripts.inbox import store as inbox_store

    member, item = mail
    member.about(item.id, "tasks/t1")
    target = InboxStore(store.redis, world="m2")
    monkeypatch.setattr(inbox_store, "now_ms", lambda: item.created_at + 7)
    moved = member.move_world(item.id, target, exits.BY, "moved")
    redis, name = store.redis, agent.name
    assert [
        (
            redis.zscore(world.key(index, name), item.id),
            redis.get(world.key("open-size", name)),
            redis.smembers(world.key("waiting")),
        )
        for world in (member, target)
        for index in ("address", "open", "pending")
    ] == [(None, "0", set())] * 3 + [(item.created_at, "1", {name})] * 3
    assert moved == replace(item, state="pending", reason="moved", updated_at=item.created_at + 7)
    assert target.get(item.id) == moved
    assert target.history(item.id)[-1]["at"] == item.created_at + 7
    assert not redis.exists(member.key("item", item.id))
    assert not redis.exists(member.key("history", item.id))
    assert (member.entry_of(item.id), target.entry_of(item.id)) == ("", "tasks/t1")


def test_moving_a_read_item_between_worlds_keeps_its_whole_history(store, agent, mail):
    member, item = mail
    member.deliver(item.id, agent.name)
    member.read(item.id, agent.name)
    before = member.history(item.id)
    target = InboxStore(store.redis, world="m2")
    member.move_world(item.id, target, exits.BY, "moved")
    assert [e["state"] for e in target.history(item.id)] == ["pending", "delivered", "read", "pending"]
    assert target.history(item.id)[:3] == before


def test_moving_a_missing_item_between_worlds_names_it(store):
    with pytest.raises(InboxError, match="no message nope"):
        InboxStore(store.redis, world="m1").move_world("nope", InboxStore(store.redis, world="m2"), exits.BY, "x")


@pytest.mark.parametrize("touched", ["item", "history", "entry"])
def test_moving_an_item_between_worlds_retries_after_a_concurrent_write(store, agent, mail, touched):
    member, item = mail
    member.about(item.id, "tasks/t1")
    writes = {
        "item": lambda: store.redis.hset(member.key("item", item.id), "reason", "changed"),
        "history": lambda: store.redis.rpush(member.key("history", item.id), '{"event": "woken"}'),
        "entry": lambda: store.redis.set(member.key("entry", item.id), "tasks/t9"),
    }
    calls = []

    def persist(pipe):
        if not calls:
            writes[touched]()
        calls.append(1)

    target = InboxStore(store.redis, world="m2", persist=persist)
    member.move_world(item.id, target, exits.BY, "moved")
    assert len(calls) == 2
    assert target.entry_of(item.id) == ("tasks/t9" if touched == "entry" else "tasks/t1")
    assert ("woken" in [e.get("event") for e in target.history(item.id)]) == (touched == "history")


def test_moving_an_item_between_worlds_gives_up_after_writes_on_every_attempt(store, agent, mail):
    member, item = mail
    target = InboxStore(
        store.redis, world="m2", persist=lambda pipe: store.redis.hset(member.key("item", item.id), "reason", "x")
    )
    with pytest.raises(InboxError, match=f"message {item.id} changed meanwhile; run the command again"):
        member.move_world(item.id, target, exits.BY, "moved")
    assert member.get(item.id).state == "pending"


def test_moving_a_delivered_item_between_worlds_returns_it_to_pending(store, agent, mail):
    member, item = mail
    member.deliver(item.id, agent.name)
    assert store.redis.zscore(member.key("delivered"), item.id) is not None
    moved = member.move_world(item.id, InboxStore(store.redis, world="m2"), exits.BY, "moved")
    assert moved.state == "pending"
    assert store.redis.zscore(member.key("delivered"), item.id) is None


def test_moving_an_item_between_worlds_runs_the_target_persist_hook(store, agent, mail):
    member, item = mail
    target = InboxStore(store.redis, world="m2", persist=lambda pipe: pipe.set("persisted", item.id))
    member.move_world(item.id, target, exits.BY, "moved")
    assert store.redis.get("persisted") == item.id


def test_the_sweep_leaves_seat_mail_whose_seat_names_another_occupant(store, moved_seat):
    member, item, _ = moved_seat
    store.seats.occupy(SEAT, "ghost", 3)
    exits.sweep(InboxStore(store.redis), "sw", store, lambda: {})
    assert [i.id for i in member.inbox(SEAT)] == [item.id]


def test_a_member_world_sweep_leaves_seat_mail_for_an_occupant_on_another_host(store, moved_seat):
    member, item, _ = moved_seat
    exits.sweep(InboxStore(store.redis, world="m1"), "sw", store, lambda: {})
    assert [i.id for i in member.inbox(SEAT)] == [item.id]


def test_the_sweep_leaves_master_seat_mail_in_its_world(store, moved_seat):
    member, _, _ = moved_seat
    seat = "master@sw"
    first = store.next_name("sw", "eng")
    store.start_execution("sw", AgentRecord(first, "eng", "t2", seat=seat, hive="m1"))
    store.seats.occupy(seat, first, 1)
    item = InboxStore(store.redis).send("sender", seat, "for the master")
    assert [i.id for i in member.inbox(seat)] == [item.id]
    [previous] = [a for a in store.agents("sw") if a.name == first]
    second = store.next_name("sw", "eng")
    store.start_execution("sw", AgentRecord(second, "eng", "t2", seat=seat, hive="m2"), previous.execution_id)
    store.seats.occupy(seat, second, 2)
    exits.sweep(InboxStore(store.redis), "sw", store, lambda: {})
    assert [i.id for i in member.inbox(seat)] == [item.id]


def test_the_sweep_moves_controller_dispatcher_seat_mail_to_its_hive_occupant(store):
    seat = "dispatcher@sw"
    item = InboxStore(store.redis).send("sender", seat, "rank this")
    controller = InboxStore(store.redis, world="")
    assert [i.id for i in controller.inbox(seat)] == [item.id]
    name = store.next_name("sw", "dispatch")
    store.start_execution("sw", AgentRecord(name, "dispatch", "dispatcher", seat=seat, hive="m1"))
    store.seats.occupy(seat, name, 1)
    exits.sweep(InboxStore(store.redis), "sw", store, lambda: {})
    assert [i.id for i in InboxStore(store.redis, world="m1").pending_mail(name)] == [item.id]
    assert controller.inbox(seat) == []
