import time

import pytest

from scripts.events import memory
from scripts.events.config import BusConfig
from scripts.hub import lease

pytestmark = pytest.mark.unit


class Clock:
    def __init__(self):
        self.now = 1_000

    def __call__(self) -> int:
        return self.now


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def store():
    return memory.open_bus(BusConfig()).state(lease.BUCKET)


def held(store, clock, holder):
    return lease.Lease(store, "reactor.timers", holder, ttl_ms=100, clock=clock)


def test_the_first_holder_takes_the_lease_and_a_second_is_refused_while_it_lives(store, clock):
    assert held(store, clock, "a").acquire() is True
    clock.now += 99
    assert held(store, clock, "b").acquire() is False
    assert lease.read(store, "reactor.timers") == lease.LeaseState("a", 1_100)


def test_the_holder_renews_its_lease_past_the_first_expiry(store, clock):
    first = held(store, clock, "a")
    first.acquire()
    clock.now += 60
    assert first.acquire() is True
    clock.now += 60
    assert held(store, clock, "b").acquire() is False
    assert lease.read(store, "reactor.timers").expires_ms == 1_160


def test_an_expired_lease_is_taken_over_and_the_old_holder_cannot_renew_it(store, clock):
    first = held(store, clock, "a")
    first.acquire()
    clock.now += 100
    assert held(store, clock, "b").acquire() is True
    assert first.acquire() is False
    assert lease.read(store, "reactor.timers").holder == "b"


def test_a_release_frees_the_lease_only_for_its_holder(store, clock):
    first, second = held(store, clock, "a"), held(store, clock, "b")
    first.acquire()
    second.release()
    assert second.acquire() is False
    first.release()
    assert lease.read(store, "reactor.timers") == lease.LeaseState("a", 0)
    assert second.acquire() is True


def test_a_release_without_a_lease_writes_nothing(store, clock):
    held(store, clock, "a").release()
    assert store.get("reactor.timers") is None


class Racing:
    def __init__(self, store, clock):
        self.store = store
        self.clock = clock
        self.raced = False

    def get(self, key):
        entry = self.store.get(key)
        if not self.raced:
            self.raced = True
            lease.Lease(self.store, key, "rival", ttl_ms=100, clock=self.clock).acquire()
        return entry

    def create(self, key, value):
        return self.store.create(key, value)

    def update(self, key, value, revision):
        return self.store.update(key, value, revision)


@pytest.mark.parametrize("prior", [None, "expired"])
def test_a_write_that_loses_the_compare_and_set_race_holds_nothing(store, clock, prior):
    if prior:
        held(store, clock, "old").acquire()
        clock.now += 200
    racing = Racing(store, clock)
    assert lease.Lease(racing, "reactor.timers", "a", ttl_ms=100, clock=clock).acquire() is False
    assert lease.read(store, "reactor.timers").holder == "rival"


def test_a_conflict_on_release_leaves_the_successor_in_place(store, clock):
    first = held(store, clock, "a")
    first.acquire()
    racing = Racing(store, clock)
    clock.now += 100
    lease.Lease(racing, "reactor.timers", "a", ttl_ms=100, clock=clock).release()
    assert lease.read(store, "reactor.timers").holder == "rival"


@pytest.mark.parametrize("ttl_ms", [0, -1, True, 1.5, "100"])
def test_a_lease_refuses_a_ttl_that_is_not_a_positive_integer(store, clock, ttl_ms):
    with pytest.raises(ValueError, match="^lease ttl must be a positive integer$"):
        lease.Lease(store, "reactor.timers", "a", ttl_ms=ttl_ms, clock=clock)


def test_a_one_millisecond_lease_is_accepted(store, clock):
    assert lease.Lease(store, "reactor.timers", "a", ttl_ms=1, clock=clock).acquire() is True


def test_lease_time_is_whole_wall_clock_milliseconds():
    before = time.time_ns() // 1_000_000
    now = lease.wall_ms()
    assert isinstance(now, int)
    assert before <= now <= time.time_ns() // 1_000_000
