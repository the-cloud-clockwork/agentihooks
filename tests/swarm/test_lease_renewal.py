from types import SimpleNamespace

import pytest

from scripts.swarm import controller, lease
from scripts.swarm.store import RedisStore, SwarmConfig, SwarmError
from tests.swarm.test_cli import env, run  # noqa: F401
from tests.swarm.test_delivery import FakeHerdr

pytestmark = [pytest.mark.unit, pytest.mark.xdist_group("fakeredis")]


@pytest.fixture
def store():
    import fakeredis

    return RedisStore(fakeredis.FakeRedis(decode_responses=True))


@pytest.fixture
def clock(monkeypatch):
    now = [1000]
    monkeypatch.setattr(lease, "now_ms", lambda saved: now[0])
    return now


def test_renew_extends_the_live_lease_and_keeps_its_epoch(store, clock):
    held = lease.acquire(store, "sw", "home")
    clock[0] = 150000
    assert lease.renew(store, "sw", held) == lease.Lease("home", 1, 330000)
    assert lease.current(store, "sw") == lease.Lease("home", 1, 330000)
    assert store.redis.pttl(store.key("sw", "control-owner")) > 179000


def test_renew_refuses_a_lease_taken_by_another_owner(store, clock):
    held = lease.acquire(store, "sw", "home")
    clock[0] = held.expires_at
    stolen = lease.acquire(store, "sw", "other")
    with pytest.raises(SwarmError) as error:
        lease.renew(store, "sw", held)
    assert str(error.value) == "the controller lease is stale"
    assert lease.current(store, "sw") == stolen


def test_renew_refuses_the_same_owner_under_a_new_epoch(store, clock):
    held = lease.acquire(store, "sw", "home")
    assert lease.release(store, "sw", held)
    again = lease.acquire(store, "sw", "home")
    with pytest.raises(SwarmError):
        lease.renew(store, "sw", held)
    assert lease.current(store, "sw") == again


def test_renew_refuses_an_expired_lease(store, clock):
    held = lease.acquire(store, "sw", "home")
    clock[0] = held.expires_at
    with pytest.raises(SwarmError):
        lease.renew(store, "sw", held)
    assert lease.current(store, "sw") is None


def test_renew_retries_a_conflicting_transaction(store, clock, monkeypatch):
    held = lease.acquire(store, "sw", "home")
    pipeline, conflicts = store.redis.pipeline, [1]

    def interleaved():
        pipe = pipeline()
        execute = pipe.execute

        def commit():
            if conflicts:
                conflicts.pop()
                store.redis.pexpire(store.key("sw", "control-owner"), lease.TTL_MS)
            return execute()

        pipe.execute = commit
        return pipe

    monkeypatch.setattr(store.redis, "pipeline", interleaved)
    clock[0] = 2000
    assert lease.renew(store, "sw", held) == lease.Lease("home", 1, 182000)


def test_a_tick_slower_than_the_lease_keeps_writing(store, clock):
    store.create(SwarmConfig("sw", ".", 1, 0))
    held = lease.acquire(store, "sw", "home")
    writes = []
    ledger = controller.FencedLedger(
        store, "sw", held, SimpleNamespace(update_task=lambda *args: writes.append(args) or "accepted")
    )
    runtime = controller.FencedRuntime(
        store,
        "sw",
        held,
        SimpleNamespace(has_capacity=lambda saved: True, spawn=lambda *args: "placed", retire=lambda agent: True),
        True,
    )
    for step in range(5):
        clock[0] += 120000
        assert ledger.update_task("sw", "t", {"step": step}) == "accepted"
    assert clock[0] - 1000 > lease.TTL_MS
    assert runtime.has_capacity(store.config("sw")) is True
    clock[0] += 120000
    assert runtime.retire("one") is True
    clock[0] += 120000
    assert runtime.spawn(store.config("sw"), "eng", "one", {"id": "t"}) == "placed"
    assert len(writes) == 5
    assert lease.current(store, "sw") == lease.Lease("home", 1, clock[0] + lease.TTL_MS)


def test_a_tick_whose_lease_was_stolen_stops(store, clock):
    store.create(SwarmConfig("sw", ".", 1, 0))
    held = lease.acquire(store, "sw", "home")
    writes, spawned = [], []
    ledger = controller.FencedLedger(store, "sw", held, SimpleNamespace(update_task=lambda *args: writes.append(args)))
    runtime = controller.FencedRuntime(
        store, "sw", held, SimpleNamespace(spawn=lambda *args: spawned.append(args), retire=spawned.append), True
    )
    ledger.update_task("sw", "t", {"step": 0})
    clock[0] += lease.TTL_MS
    stolen = lease.acquire(store, "sw", "other")
    for write in (
        lambda: ledger.update_task("sw", "t", {"step": 1}),
        lambda: runtime.spawn(store.config("sw"), "eng", "one", {"id": "t"}),
        lambda: runtime.retire("one"),
    ):
        with pytest.raises(SwarmError) as error:
            write()
        assert str(error.value) == "the controller lease is stale"
    assert len(writes) == 1
    assert spawned == []
    assert lease.current(store, "sw") == stolen


def test_run_tick_slower_than_the_lease_finishes(env, clock, monkeypatch):  # noqa: F811
    from scripts.swarm import cli

    store, ledger, rt = env
    run("sw", "create", "--repo", "/repo")
    started = clock[0]

    def slow(*args):
        clock[0] += 120000
        return []

    monkeypatch.setattr(cli.phase_planning, "planning_pass", slow)
    monkeypatch.setattr(cli.wake, "wake_pass", slow)
    cli.run_tick(store, "sw", ledger, rt, FakeHerdr({}))
    assert clock[0] - started > lease.TTL_MS
    assert lease.current(store, "sw").epoch == 1
    assert store.redis.get(store.key("sw", "last-tick"))
