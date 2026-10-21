import multiprocessing
from types import SimpleNamespace

import pytest

from scripts.swarm import commands, controller, lease
from scripts.swarm.rename import LOCK_MS, rename_swarm
from scripts.swarm.store import AgentRecord, RedisStore, SwarmConfig, SwarmError

pytestmark = pytest.mark.unit

LEDGER = SimpleNamespace(rename_agent=lambda slug, old, new: None)


@pytest.fixture
def store():
    import fakeredis

    store = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    store.create(SwarmConfig("sw", "/repo", 1, 0))
    store.put_agent("sw", AgentRecord("sw-eng-1", "eng", "t1", pane_id="w1:p1"))
    return store


def rename_until_killed(store, sender):
    def herdr(args):
        sender.send(store.redis.get(store.key("sw", "tick-lock")))
        sender.recv()

    rename_swarm(store, "sw", LEDGER, SimpleNamespace(herdr=herdr), 10)


def test_a_rename_killed_while_holding_the_tick_lock_is_replaced_at_once(store):
    held = lease.acquire(store, "sw", commands.hive_id())
    receiver, sender = multiprocessing.Pipe()
    child = multiprocessing.get_context("fork").Process(target=rename_until_killed, args=(store, sender))
    child.start()
    try:
        assert receiver.poll(10)
        original = receiver.recv()
        store.redis.set(store.key("sw", "tick-lock"), original, px=600_000)
        assert controller.take_tick_lock(store, "sw", held, 600_000) is None
        child.kill()
        child.join(10)
        assert child.exitcode == -9
        replacement = controller.take_tick_lock(store, "sw", held, 600_000)
        assert replacement is not None and replacement != original
        assert store.redis.get(store.key("sw", "tick-lock")) == replacement
    finally:
        if child.is_alive():
            child.kill()
        child.join(10)
        receiver.close()
        sender.close()


def test_a_rename_releases_the_tick_lock_it_took(store):
    panes = {"agents": [{"pane_id": "w1:p1", "name": "sw-eng-1", "workspace_id": "w1"}]}
    held_during = []

    def herdr(args):
        held_during.append(store.redis.exists(store.key("sw", "tick-lock")))
        return {**panes, "workspaces": []}

    rename_swarm(store, "sw", LEDGER, SimpleNamespace(herdr=herdr), 10)
    assert held_during and all(held_during)
    assert store.redis.get(store.key("sw", "tick-lock")) is None
    held = lease.acquire(store, "sw", commands.hive_id())
    assert controller.take_tick_lock(store, "sw", held, 600_000) is not None


def test_a_rename_holds_the_tick_lock_for_the_rename_lock_time(store, monkeypatch):
    taken = []
    take = controller.take_tick_lock

    def spy(store, slug, held, ttl_ms):
        taken.append(ttl_ms)
        return take(store, slug, held, ttl_ms)

    def herdr(args):
        raise SwarmError("pane gone")

    monkeypatch.setattr(controller, "take_tick_lock", spy)
    with pytest.raises(SwarmError, match="pane gone"):
        rename_swarm(store, "sw", LEDGER, SimpleNamespace(herdr=herdr), 10)
    assert taken == [LOCK_MS]


def test_a_rename_leaves_a_tick_lock_it_lost_to_another_holder(store):
    def herdr(args):
        store.redis.set(store.key("sw", "tick-lock"), "another", px=600_000)
        raise SwarmError("pane gone")

    with pytest.raises(SwarmError, match="pane gone"):
        rename_swarm(store, "sw", LEDGER, SimpleNamespace(herdr=herdr), 10)
    assert store.redis.get(store.key("sw", "tick-lock")) == "another"


def test_a_rename_refuses_a_swarm_that_belongs_to_another_hive(store):
    assert lease.acquire(store, "sw", "another-hive") is not None
    with pytest.raises(SwarmError, match="another hive"):
        rename_swarm(store, "sw", LEDGER, None, 10)
    assert store.redis.get(store.key("sw", "tick-lock")) is None


def test_a_rename_refuses_a_swarm_whose_tick_holds_the_lock(store):
    held = lease.acquire(store, "sw", commands.hive_id())
    tick = controller.take_tick_lock(store, "sw", held, 600_000)
    with pytest.raises(SwarmError, match="another tick or rename is running"):
        rename_swarm(store, "sw", LEDGER, None, 10)
    assert store.redis.get(store.key("sw", "tick-lock")) == tick


def two_agent_herdr(store, on_rename):
    store.put_agent("sw", AgentRecord("sw-eng-2", "eng", "t2", pane_id="w1:p2"))
    panes = [
        {"pane_id": "w1:p1", "name": "sw-eng-1", "workspace_id": "w1"},
        {"pane_id": "w1:p2", "name": "sw-eng-2", "workspace_id": "w1"},
    ]
    renamed = []

    def herdr(args):
        if args[:2] == ["agent", "rename"]:
            renamed.append(args[2])
            on_rename(args[2])
        return {"agents": panes, "workspaces": []}

    return SimpleNamespace(herdr=herdr), renamed


def test_a_long_rename_keeps_its_lease(store, monkeypatch):
    import time

    monkeypatch.setenv("AGENTIHOOKS_CONTROLLER_TICK_SECONDS", "1")
    runtime, renamed = two_agent_herdr(store, lambda pane: time.sleep(1.6))
    rename_swarm(store, "sw", LEDGER, runtime, 10)
    assert renamed == ["w1:p1", "w1:p2"]
    live = lease.current(store, "sw")
    assert live is not None and live.owner == commands.hive_id()
    assert lease.acquire(store, "sw", "another-hive") is None


def test_a_rename_that_lost_its_lease_stops_before_the_next_write(store):
    def take_over(pane):
        store.redis.delete(store.key("sw", "control-owner"))
        assert lease.acquire(store, "sw", "another-hive") is not None

    runtime, renamed = two_agent_herdr(store, take_over)
    with pytest.raises(SwarmError, match="the controller lease is stale"):
        rename_swarm(store, "sw", LEDGER, runtime, 10)
    assert renamed == ["w1:p1"]
    assert set(store.redis.hkeys(store.key("sw", "agents"))) == {"sw-eng-1", "sw-eng-2"}
    assert lease.current(store, "sw").owner == "another-hive"
