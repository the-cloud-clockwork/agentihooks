from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest

from scripts.routing import reservation


@pytest.fixture
def clock(monkeypatch):
    now = [100.0]
    monkeypatch.setattr("time.time", lambda: now[0])
    return now


@pytest.fixture
def redis(clock):
    import fakeredis

    return fakeredis.FakeRedis(decode_responses=True)


def test_concurrent_reservations_at_cap_one_have_one_winner(redis):
    slots = reservation.LaunchSlots(redis)
    ready = Barrier(2)

    def take(execution):
        ready.wait()
        return slots.reserve("claude:one", execution, 1, 1000)

    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(take, ["first", "second"]))
    assert sorted(results) == [False, True]
    assert slots.counts() == {"claude:one": 1}


def test_reservation_reading_retains_the_atomic_refusal_count(redis):
    slots = reservation.LaunchSlots(redis)
    assert slots.reserve_reading("claude:one", "first", 2, 1000) == reservation.SlotReading(True, 0)
    assert slots.reserve("claude:one", "second", 2, 1000) is True
    refused = slots.reserve_reading("claude:one", "third", 2, 1000)
    assert slots.release("first") is True
    assert refused == reservation.SlotReading(False, 2)
    assert slots.counts() == {"claude:one": 1}
    assert slots.reserve("claude:one", "third", 2, 1000) is True


def test_expiry_and_exit_release_capacity(redis, clock):
    slots = reservation.LaunchSlots(redis)
    assert slots.reserve("claude:one", "first", 1, 1000)
    assert slots.reserve("claude:one", "first", 1, 1000)
    assert not slots.reserve("claude:one", "second", 1, 1000)
    clock[0] += 1.001
    assert slots.counts() == {}
    assert slots.reserve("claude:one", "second", 1, 1000)
    assert slots.release("second")
    assert not slots.release("second")
    assert slots.counts() == {}
    assert slots.reserve("claude:one", "third", 1, 1000)


def test_a_live_hive_keeps_its_session_report_counted_until_its_heartbeat_goes_stale(redis, clock):
    import json

    from scripts.hive import registry

    redis.sadd(registry.INDEX, "h1")
    redis.hset(registry.key("h1"), "heartbeat_at", int(clock[0] * 1000))
    redis.set(f"{registry.key('h1')}:sessions", json.dumps({"codex:one": 6}), ex=45)
    slots = reservation.LaunchSlots(redis)
    for _ in range(6):
        clock[0] += 10
        registry.touch(redis, "h1", int(clock[0] * 1000))
    assert slots.reserve_reading("codex:one", "late", 6, 1000) == reservation.SlotReading(False, 6)
    redis.expire(f"{registry.key('h1')}:sessions", 600)
    clock[0] += registry.LIVE_MS / 1000 + 1
    assert slots.reserve_reading("codex:one", "late", 6, 1000) == reservation.SlotReading(True, 0)


@pytest.mark.parametrize("cap,ttl", [(-1, 1), (True, 1), (1, 0), (1, 300001), (1, True), (1.0, 1), (1, 1.0)])
def test_invalid_launch_bounds_are_refused(redis, cap, ttl):
    with pytest.raises(ValueError, match="invalid launch slot cap or lifetime"):
        reservation.LaunchSlots(redis).reserve("claude:one", "first", cap, ttl)


def test_acknowledgement_and_heartbeat_refresh_are_bounded(redis, clock):
    slots = reservation.LaunchSlots(redis)
    assert not slots.acknowledge("missing")
    assert not slots.reserve("claude:one", "none", 0, 1000)
    assert slots.reserve("claude:one", "first", 1, 1)
    assert slots.acknowledge("first")
    assert slots.acknowledge("first")
    clock[0] += 44
    slots.refresh(["first", "missing"])
    clock[0] += 1
    assert slots.counts() == {"claude:one": 1}
    assert slots.counts({"first"}) == {}
    clock[0] += 44.0005
    assert slots.counts() == {}
    assert not slots.release("first")
    assert not slots.acknowledge("first")


def test_pending_refresh_and_cross_account_retry_do_not_take_new_room(redis, clock):
    slots = reservation.LaunchSlots(redis)
    assert slots.reserve("claude:one", "first", 1, 1000)
    assert not slots.reserve("codex:one", "first", 1, 1000)
    slots.refresh(["first"])
    clock[0] += 1.0005
    assert slots.counts() == {}
    assert slots.reserve("codex:one", "second", 1, 300000)
    assert slots.reserve("claude:one", "third", 1, 1000)
    assert slots.counts() == {"codex:one": 1, "claude:one": 1}


@pytest.mark.parametrize("reported", [None, {}, {"masters": 2}])
def test_global_master_ceiling_combines_hive_and_durable_counts(monkeypatch, reported):
    from types import SimpleNamespace

    from scripts.routing import aggregate
    from scripts.routing.master_account import MasterAccount

    environ = {"AGENTIHOOKS_RUNTIME_BACKEND": "kubernetes"}
    monkeypatch.setenv("AGENTIHOOKS_RUNTIME_BACKEND", "local")
    monkeypatch.setattr("time.time", lambda: 100)
    monkeypatch.setattr(reservation.master_account, "load", lambda env: {"codex": MasterAccount("codex", "one")})
    agent = SimpleNamespace(lane="master", state="working", harness="codex", account="one", route_kind="subscription")
    store = SimpleNamespace(slugs=lambda: ["swarm"], agents=lambda slug: [agent])

    def observed(now):
        assert now == 100
        return {} if reported is None else {"codex:one": reported}

    monkeypatch.setattr(aggregate, "current", observed)
    held = reservation.current("codex", environ, store_of=lambda env: store)
    assert held == reservation.Reservation("codex", "one", 3 if reported else 1)
