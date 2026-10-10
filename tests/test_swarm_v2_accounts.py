import json
import threading
from dataclasses import replace
from pathlib import Path

import pytest
from redis.exceptions import WatchError

from scripts.swarm.keyspace import ROOT
from scripts.swarm.store import SwarmError
from scripts.swarm_v2 import accounts
from scripts.swarm_v2.accounts import OCCUPIED, RESERVED, AccountCapacity, Slot
from scripts.swarm_v2.registry import FleetRegistry, Scope, Session
from tests.sv2_ctl02_cases import build

pytestmark = pytest.mark.unit

INPUTS = json.loads((Path(__file__).parent / "fixtures/swarm_v2/account-capacity.json").read_text())
SLUG = "fixture"
ACCOUNT = INPUTS["account"]
CAP = INPUTS["cap"]
TTL = INPUTS["ttl_ms"]
HELD, FIRST, SECOND = INPUTS["held_seat"], *INPUTS["requests"]
MACHINE = Scope(**INPUTS["machine"])


class Racing:
    """A store whose next transaction commits the other controller's write between its read and its commit."""

    def __init__(self, store, write):
        self.store, self.write = store, write

    @property
    def redis(self):
        return self

    def key(self, *parts):
        return self.store.key(*parts)

    def pipeline(self):
        pipe = self.store.redis.pipeline()
        real = pipe.execute

        def execute():
            if self.write:
                write, self.write = self.write, None
                write()
            return real()

        pipe.execute = execute
        return pipe

    def __getattr__(self, name):
        return getattr(self.store.redis, name)


class Flaky(Racing):
    def __init__(self, store, failures):
        super().__init__(store, None)
        self.failures = failures

    def pipeline(self):
        pipe = self.store.redis.pipeline()
        real = pipe.execute

        def execute():
            if self.failures:
                self.failures -= 1
                raise WatchError
            return real()

        pipe.execute = execute
        return pipe


class World:
    def __init__(self, monkeypatch):
        self.store, self.authority, _, self.clock, self.start = build(monkeypatch)
        self.fleet = FleetRegistry(self.store, SLUG, self.authority.authorize, lambda: self.clock[0])
        self.capacity = self.controller()
        self.agents, self.tokens = {}, {}

    def controller(self, store=None):
        return AccountCapacity(store or self.store, SLUG, self.authority.authorize, lambda: self.clock[0])

    def launch(self, seat, previous=""):
        agent, token = self.start(seat=seat, previous=previous)
        self.agents[seat], self.tokens[seat] = agent, token
        return token

    def session(self, seat):
        agent = self.agents[seat]
        return Session(
            f"sess-{agent.name}", MACHINE, 4242, 7, "claude", agent.name, "", agent.execution_id, agent.generation
        )

    def running(self, seat):
        token = self.launch(seat)
        self.capacity.reserve(token, CAP, TTL)
        self.fleet.register(self.session(seat), token)
        return self.capacity.occupy(token, MACHINE, self.session(seat).session_id)

    def rows(self):
        return self.store.redis.hgetall(f"{ROOT}:accounts:{ACCOUNT}")

    def holders(self):
        return sorted(slot.holder for slot in self.capacity.slots(ACCOUNT))


@pytest.fixture
def world(monkeypatch):
    return World(monkeypatch)


def refusal(call, *args):
    with pytest.raises(SwarmError) as error:
        call(*args)
    return str(error.value)


@pytest.mark.parametrize("run", ["first", "second"])
def test_two_concurrent_controllers_cannot_both_take_the_last_slot(world, run):
    held = world.running(HELD)
    first, second = world.launch(FIRST), world.launch(SECOND)
    won = []
    racing = world.controller(Racing(world.store, lambda: won.append(world.capacity.reserve(second, CAP, TTL))))

    assert refusal(racing.reserve, first, CAP, TTL) == "account_full"

    assert won == [Slot(ACCOUNT, f"{SLUG}/{SECOND}", world.agents[SECOND].execution_id, 1, RESERVED, 1000 + TTL)]
    assert world.holders() == [f"{SLUG}/{HELD}", f"{SLUG}/{SECOND}"]
    assert held.state == OCCUPIED
    stored = json.loads(world.rows()[f"{SLUG}/{SECOND}"])
    assert (stored["execution_id"], stored["state"]) == (world.agents[SECOND].execution_id, "reserved")
    assert json.loads(world.rows()[f"{SLUG}/{HELD}"])["state"] == "occupied"
    assert world.capacity.account_reservation_conflicts_total() == 1
    assert world.capacity.account_reservation_conflicts() == {ACCOUNT: 1}
    assert world.store.redis.hgetall(f"{ROOT}:account-reservation-conflicts") == {ACCOUNT: "1"}


def test_an_account_with_no_slots_refuses_without_writing(world):
    token = world.launch(FIRST)

    assert refusal(world.capacity.reserve, token, 0, TTL) == "account_full"
    assert world.rows() == {}
    assert world.capacity.account_reservation_conflicts_total() == 1


def test_simultaneous_launch_threads_take_one_slot(world):
    world.running(HELD)
    tokens = [world.launch(FIRST), world.launch(SECOND)]
    gate = threading.Barrier(2)
    outcomes = []

    def request(token):
        controller = world.controller()
        gate.wait()
        try:
            outcomes.append(controller.reserve(token, CAP, TTL).holder)
        except SwarmError as error:
            outcomes.append(str(error))

    threads = [threading.Thread(target=request, args=(token,)) for token in tokens]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert sorted(outcomes, key=lambda outcome: outcome != "account_full")[0] == "account_full"
    assert len(world.holders()) == CAP
    assert world.capacity.account_reservation_conflicts_total() == 1


def test_a_stale_attempt_cannot_free_the_slot_its_replacement_holds(world):
    world.running(HELD)
    old = world.launch(FIRST)
    world.capacity.reserve(old, CAP, TTL)
    cached = world.authority.authorize(old)
    replacement = world.launch(FIRST, previous=world.agents[FIRST].execution_id)
    current = world.capacity.reserve(replacement, CAP, TTL)
    stale = AccountCapacity(world.store, SLUG, lambda _: cached, lambda: world.clock[0])
    before = world.rows()

    assert refusal(stale.release, old) == "stale_generation"
    assert refusal(stale.reserve, old, CAP, TTL) == "stale_generation"
    assert refusal(stale.occupy, old, MACHINE, "sess-old") == "stale_generation"
    assert world.rows() == before
    assert current.generation == world.agents[FIRST].generation == 2
    assert world.capacity.slots(ACCOUNT)[-1] == current
    assert world.capacity.account_reservation_conflicts_total() == 0


def test_a_replacement_reuses_its_seat_slot_on_a_full_account(world):
    world.running(HELD)
    old = world.launch(FIRST)
    world.capacity.reserve(old, CAP, TTL)
    replacement = world.launch(FIRST, previous=world.agents[FIRST].execution_id)

    slot = world.capacity.reserve(replacement, CAP, TTL)

    assert (slot.holder, slot.generation) == (f"{SLUG}/{FIRST}", 2)
    assert world.holders() == [f"{SLUG}/{HELD}", f"{SLUG}/{FIRST}"]


def test_another_execution_at_the_held_generation_cannot_take_the_slot(world):
    token = world.launch(FIRST)
    world.capacity.reserve(token, CAP, TTL)
    grant = replace(world.authority.authorize(token), execution_id="exe-other")
    other = AccountCapacity(world.store, SLUG, lambda _: grant, lambda: world.clock[0])
    before = world.rows()

    assert refusal(other.reserve, token, CAP, TTL) == "stale_generation"
    assert refusal(other.release, token) == "stale_generation"
    assert world.rows() == before


def test_a_worker_that_never_starts_stops_counting_at_its_reservation_expiry(world):
    world.running(HELD)
    first, second = world.launch(FIRST), world.launch(SECOND)
    world.capacity.reserve(first, CAP, TTL)
    assert refusal(world.capacity.reserve, second, CAP, TTL) == "account_full"

    world.clock[0] += TTL - 1
    assert world.holders() == [f"{SLUG}/{HELD}", f"{SLUG}/{FIRST}"]
    world.clock[0] += 1
    assert world.holders() == [f"{SLUG}/{HELD}"]

    taken = world.capacity.reserve(second, CAP, TTL)
    assert world.capacity.reserve(second, CAP, TTL) == taken
    assert sorted(world.rows()) == [f"{SLUG}/{HELD}", f"{SLUG}/{SECOND}"]
    assert refusal(world.capacity.occupy, first, MACHINE, "sess-never") == "reservation_expired"
    assert world.capacity.account_reservation_conflicts_total() == 1


def test_an_expired_own_reservation_is_taken_again_when_there_is_room(world):
    token = world.launch(FIRST)
    world.capacity.reserve(token, CAP, TTL)
    world.clock[0] += TTL
    assert refusal(world.capacity.occupy, token, MACHINE, "sess-x") == "reservation_expired"

    again = world.capacity.reserve(token, CAP, TTL)

    assert (again.state, again.expires_ms) == (RESERVED, 1000 + 2 * TTL)
    assert refusal(world.capacity.occupy, token, MACHINE, "sess-x") == "unregistered"


def test_occupancy_needs_a_live_registry_record_of_the_same_generation(world):
    token = world.launch(FIRST)
    reserved = world.capacity.reserve(token, CAP, TTL)
    session = world.session(FIRST)

    assert refusal(world.capacity.occupy, token, MACHINE, session.session_id) == "unregistered"
    world.fleet.register(session, token)
    occupied = world.capacity.occupy(token, MACHINE, session.session_id)

    assert occupied == replace(reserved, state=OCCUPIED, expires_ms=0, session=session.key())
    assert world.capacity.occupy(token, MACHINE, session.session_id) == occupied
    assert refusal(world.capacity.occupy, token, MACHINE, "sess-other") == "registration_conflict"
    world.clock[0] += 10 * TTL
    assert world.capacity.slots(ACCOUNT) == [occupied]


def test_a_closed_or_foreign_registry_record_cannot_confirm_occupancy(world):
    token = world.launch(FIRST)
    world.capacity.reserve(token, CAP, TTL)
    other = world.launch(SECOND)
    world.fleet.register(world.session(SECOND), other)
    world.fleet.register(world.session(FIRST), token)
    world.fleet.close(MACHINE, world.session(FIRST).session_id, token)

    assert refusal(world.capacity.occupy, token, MACHINE, world.session(FIRST).session_id) == "unregistered"
    assert refusal(world.capacity.occupy, token, MACHINE, world.session(SECOND).session_id) == "unregistered"
    assert world.capacity.slots(ACCOUNT)[0].state == RESERVED


def test_release_frees_only_its_own_slot(world):
    world.running(HELD)
    token = world.launch(FIRST)
    slot = world.capacity.reserve(token, CAP, TTL)

    assert world.capacity.release(token) == slot
    assert world.capacity.release(token) is None
    assert world.holders() == [f"{SLUG}/{HELD}"]


def test_freeze_refuses_new_reservations_and_reconstruct_rebuilds_occupancy(world):
    held = world.running(HELD)
    pending = world.capacity.reserve(world.launch(FIRST), CAP, TTL)
    blocked = world.launch(SECOND)
    phantom = Slot(ACCOUNT, f"{SLUG}/eng-9@fixture", "exe-phantom", 1, OCCUPIED, 0, "gone")
    foreign = Slot("other", "elsewhere/eng-1@elsewhere", "exe-foreign", 1, OCCUPIED, 0, "kept")
    world.store.redis.hdel(f"{ROOT}:accounts:{ACCOUNT}", held.holder)
    world.store.redis.hset(f"{ROOT}:accounts:{ACCOUNT}", phantom.holder, accounts.encode(phantom))
    world.store.redis.hset(f"{ROOT}:accounts:other", foreign.holder, accounts.encode(foreign))
    tokens = {agent.execution_id: world.tokens[seat] for seat, agent in world.agents.items()}

    def registration(execution_id):
        return world.authority.authorize(tokens[execution_id])

    world.capacity.freeze()
    assert world.capacity.frozen()
    assert world.store.redis.get(f"{ROOT}:accounts-frozen") == "1"
    assert refusal(world.capacity.reserve, blocked, CAP + 1, TTL) == "reservations_frozen"

    assert world.capacity.reconstruct(world.fleet, registration) == {ACCOUNT: 1}
    assert world.capacity.slots(ACCOUNT) == [held, pending]
    assert world.store.redis.hgetall(f"{ROOT}:accounts:other") == {foreign.holder: accounts.encode(foreign)}
    assert world.capacity.reconstruct(world.fleet, registration) == {ACCOUNT: 1}
    world.capacity.thaw()
    assert not world.capacity.frozen()
    assert refusal(world.capacity.reserve, blocked, CAP, TTL) == "account_full"


def test_reconstruct_skips_records_without_a_matching_registration(world):
    world.running(HELD)
    token = world.tokens[HELD]
    grant = world.authority.authorize(token)
    world.store.redis.delete(f"{ROOT}:accounts:{ACCOUNT}")

    assert world.capacity.reconstruct(world.fleet, lambda _: None) == {}
    assert world.capacity.reconstruct(world.fleet, lambda _: replace(grant, generation=9)) == {}
    world.fleet.close(MACHINE, world.session(HELD).session_id, token)
    assert world.capacity.reconstruct(world.fleet, lambda _: grant) == {}
    assert world.capacity.slots(ACCOUNT) == []


def test_reconstruct_confirms_a_reserved_record_and_keeps_a_newer_reservation(world):
    reserved = world.capacity.reserve(world.launch(HELD), CAP, TTL)
    world.fleet.register(world.session(HELD), world.tokens[HELD])
    old = world.launch(FIRST)
    world.capacity.reserve(old, CAP, TTL)
    world.fleet.register(world.session(FIRST), old)
    grants = {world.agents[seat].execution_id: world.authority.authorize(world.tokens[seat]) for seat in (HELD, FIRST)}
    newer = world.capacity.reserve(world.launch(FIRST, previous=world.agents[FIRST].execution_id), CAP, TTL)
    stray = Slot("other", f"{SLUG}/eng-9@fixture", "exe-stray", 1, OCCUPIED, 0, "gone")
    world.store.redis.hset(f"{ROOT}:accounts:other", stray.holder, accounts.encode(stray))

    rebuilt = world.capacity.reconstruct(world.fleet, grants.get)

    session = world.session(HELD).key()
    assert rebuilt == {ACCOUNT: 2}
    assert world.capacity.slots(ACCOUNT) == [replace(reserved, state=OCCUPIED, expires_ms=0, session=session), newer]
    assert world.store.redis.hgetall(f"{ROOT}:accounts:other") == {}


@pytest.mark.parametrize(("cap", "ttl"), [(-1, TTL), (True, TTL), (CAP, 0), (CAP, 1.5), (CAP, False)])
def test_invalid_caps_and_lifetimes_are_refused(world, cap, ttl):
    token = world.launch(FIRST)

    assert refusal(world.capacity.reserve, token, cap, ttl) == "invalid_request"
    assert world.rows() == {}


def test_a_request_without_a_grant_of_this_swarm_is_refused(world):
    token = world.launch(FIRST)
    grant = replace(world.authority.authorize(token), swarm_id="elsewhere")

    assert refusal(world.capacity.reserve, "", CAP, TTL) == "forbidden_scope"
    assert refusal(AccountCapacity(world.store, SLUG, lambda _: grant).reserve, token, CAP, TTL) == "forbidden_scope"
    assert refusal(AccountCapacity(world.store, SLUG, lambda _: None).release, token) == "forbidden_scope"
    assert world.rows() == {}


def test_writes_retry_a_watch_conflict_and_give_up_after_their_attempts(world):
    token = world.launch(FIRST)

    assert world.controller(Flaky(world.store, 4)).reserve(token, CAP, TTL).holder
    world.capacity.release(token)
    assert refusal(world.controller(Flaky(world.store, 5)).reserve, token, CAP, TTL) == ("dependency_unavailable")
    assert world.rows() == {}


def test_the_default_clock_is_the_redis_server_clock(world):
    token = world.launch(FIRST)
    seconds, micros = world.store.redis.time()

    slot = AccountCapacity(world.store, SLUG, world.authority.authorize).reserve(token, CAP, TTL)

    assert seconds * 1000 + micros // 1000 + TTL <= slot.expires_ms <= seconds * 1000 + micros // 1000 + TTL + 5000


def test_a_slot_round_trips_through_its_stored_document():
    slot = Slot(ACCOUNT, f"{SLUG}/{FIRST}", "exe-1", 3, OCCUPIED, 0, "abc")

    assert accounts.decode(accounts.encode(slot)) == slot
