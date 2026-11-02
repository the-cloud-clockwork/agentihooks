from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import replace

import pytest

from scripts.swarm import lease
from scripts.swarm.store import RedisStore, SwarmConfig
from scripts.tick.adapters import coordination as coordination_adapter
from scripts.tick.deadline import Deadline
from scripts.tick.ports import Coordination

pytestmark = pytest.mark.unit


@pytest.fixture
def store() -> RedisStore:
    import fakeredis

    saved = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    saved.create(SwarmConfig("sw", "/repo", 2, 1))
    return saved


def _clock(now: list[float]) -> Callable[[], float]:
    return lambda: now[0]


def _deadline(seconds: float = 5.0) -> Deadline:
    return Deadline(seconds, _clock([0.0]))


def _adapter(store: RedisStore) -> coordination_adapter.StoreCoordination:
    return coordination_adapter.StoreCoordination(store, "sw", "home")


def test_the_store_coordination_answers_the_coordination_port(store: RedisStore) -> None:
    assert isinstance(_adapter(store), Coordination)


def test_read_returns_the_swarm_config(store: RedisStore) -> None:
    assert _adapter(store).read(deadline=_deadline()) == store.config("sw")


def test_lease_acquires_the_controller_lease_for_its_owner_and_refuses_another(store: RedisStore) -> None:
    held = _adapter(store).lease(deadline=_deadline())
    assert held is not None
    assert (held.owner, held.epoch) == ("home", 1)
    assert lease.current(store, "sw") == held
    assert coordination_adapter.StoreCoordination(store, "sw", "other").lease(deadline=_deadline()) is None


def test_lock_holds_the_named_lock_until_the_deadline_ends(store: RedisStore) -> None:
    adapter = _adapter(store)
    token = adapter.lock("placement", deadline=Deadline(2.5, _clock([0.0])))
    assert token is not None
    key = store.key("sw", "lock", "placement")
    assert store.redis.get(key) == token
    assert 2400 < store.redis.pttl(key) <= 2500
    assert adapter.lock("placement", deadline=_deadline()) is None
    assert adapter.lock("other", deadline=_deadline()) not in (None, token)


def test_a_lock_with_under_a_millisecond_left_still_expires(store: RedisStore) -> None:
    token = _adapter(store).lock("short", deadline=Deadline(0.0004, _clock([0.0])))
    assert token is not None
    assert 0 < store.redis.pttl(store.key("sw", "lock", "short")) <= 1


def test_configure_stores_only_the_settings_that_changed(store: RedisStore, monkeypatch: pytest.MonkeyPatch) -> None:
    updates: list[dict[str, object]] = []
    update = store.update
    monkeypatch.setattr(store, "update", lambda slug, **changes: updates.append(changes) or update(slug, **changes))
    adapter = _adapter(store)
    current = store.config("sw")
    assert adapter.configure(current, deadline=_deadline()) == current
    assert updates == []
    changed = adapter.configure(replace(current, max_eng=5, state="stopped"), deadline=_deadline())
    assert updates == [{"max_eng": 5, "state": "stopped"}]
    assert (changed.max_eng, changed.state) == (5, "stopped")
    assert store.config("sw") == changed


def test_capacity_counts_the_launch_slots_in_use(store: RedisStore) -> None:
    from scripts.swarm.keyspace import ROOT

    for execution, slot, state in (("a", "home", "starting"), ("b", "home", "starting"), ("c", "hive", "starting")):
        store.redis.sadd(f"{ROOT}:launch-slots", execution)
        store.redis.set(f"{ROOT}:launch-slot:{execution}", json.dumps({"slot": slot, "state": state}))
    assert _adapter(store).capacity(deadline=_deadline()) == {"home": 2, "hive": 1}


def test_mark_sets_a_swarm_mark_and_none_drops_it(store: RedisStore) -> None:
    adapter = _adapter(store)
    key = store.key("sw", "last-tick")
    assert adapter.mark("last-tick", 1234, deadline=_deadline()) is True
    assert store.redis.get(key) == "1234"
    assert adapter.mark("last-tick", None, deadline=_deadline()) is True
    assert store.redis.get(key) is None
    assert adapter.mark("last-tick", None, deadline=_deadline()) is False


@pytest.mark.parametrize(
    "call",
    [
        lambda adapter, deadline: adapter.read(deadline=deadline),
        lambda adapter, deadline: adapter.lease(deadline=deadline),
        lambda adapter, deadline: adapter.lock("tick", deadline=deadline),
        lambda adapter, deadline: adapter.configure(adapter.store.config("sw"), deadline=deadline),
        lambda adapter, deadline: adapter.capacity(deadline=deadline),
        lambda adapter, deadline: adapter.mark("flag", 1, deadline=deadline),
    ],
)
def test_an_expired_deadline_changes_nothing(store: RedisStore, call: Callable[..., object]) -> None:
    before = sorted(store.redis.keys("*"))
    with pytest.raises(TimeoutError, match="^tool deadline expired$"):
        call(_adapter(store), Deadline(1.0, _clock([1.0])))
    assert sorted(store.redis.keys("*")) == before
