import json
from dataclasses import replace

import fakeredis
import pytest
from scripts.swarm_v2.launch import DistributedLaunch, Launch, LaunchTerms

from scripts.swarm.keyspace import ROOT
from scripts.swarm.store import AgentRecord, RedisStore, SwarmConfig, SwarmError
from scripts.swarm_v2.accounts import OCCUPIED, RESERVED, AccountCapacity, Slot
from scripts.swarm_v2.auth_context import GrantRefused, LaunchAuthority, LaunchKey
from scripts.swarm_v2.controller import Controller
from scripts.swarm_v2.registry import LIVE, FleetRegistry, Scope, Session
from scripts.swarm_v2.runtime.base import Capability, Outcome, RuntimeRouter, SpawnRequest, Status

pytestmark = pytest.mark.unit

SLUG = "fixture"
ACCOUNT = "fixture"
BACKEND = "kubernetes"
TTL = 30000
PROJECT = "github.com/the-cloud-clockwork/agentihooks"
FIRST, SECOND = "eng-1@fixture", "eng-2@fixture"
MACHINE = Scope(BACKEND, "pod-0000000000000000000000000000c003", "boot-worker/pid:[4026531836]")


class Remote:
    backend = BACKEND
    capabilities = frozenset({Capability.SPAWN})

    def __init__(self, status=Status.OK, during=None):
        self.status, self.during, self.requests = status, during, []

    def spawn(self, request):
        self.requests.append(request)
        if self.during:
            during, self.during = self.during, None
            during(request)
        return Outcome("spawn", self.status, BACKEND, f"placed-{request.name}" if self.status is Status.OK else None)


class World:
    def __init__(self, monkeypatch, cap=1, runtime=None):
        self.store = RedisStore(fakeredis.FakeRedis(decode_responses=True))
        self.store.create(SwarmConfig(SLUG, "agentihooks", 2, 0))
        self.clock = [1000]
        monkeypatch.setattr("time.time", lambda: self.clock[0] / 1000)
        self.controller = Controller(self.store, SLUG, [], lambda: True)
        assert self.controller.acquire()
        self.grants = LaunchAuthority(
            self.store, LaunchKey("fixture-key", b"x" * 32), "controller", "claims", lambda: self.clock[0] / 1000
        )

        def verify(token):
            return self.grants.verify(SLUG, token)

        self.capacity = AccountCapacity(self.store, SLUG, verify, lambda: self.clock[0])
        self.fleet = FleetRegistry(self.store, SLUG, verify, lambda: self.clock[0])
        self.runtime = runtime or Remote()
        self.router = RuntimeRouter([self.runtime], BACKEND)
        self.launcher = DistributedLaunch(self.controller, self.grants, self.capacity, self.fleet, self.router)
        self.terms = LaunchTerms(ACCOUNT, cap, TTL, (PROJECT,), "swarm")

    def launch(self, seat, previous=""):
        agent = AgentRecord(self.store.next_name(SLUG, "eng"), "eng", "task", seat=seat)
        request = SpawnRequest(None, "eng", agent.name, {"id": "task"})
        return self.launcher.spawn(request, agent, self.terms, previous)

    def session(self, launch):
        agent = launch.agent
        return Session(
            f"sess-{agent.name}", MACHINE, 4242, 7, "claude", agent.name, "", agent.execution_id, agent.generation
        )

    def rows(self):
        return {
            holder: json.loads(raw) for holder, raw in self.store.redis.hgetall(f"{ROOT}:accounts:{ACCOUNT}").items()
        }


@pytest.fixture
def world(monkeypatch):
    return World(monkeypatch)


def test_a_launch_reserves_its_slot_before_the_runtime_is_called(monkeypatch):
    seen = []
    world = World(monkeypatch, runtime=Remote(during=lambda request: seen.append(world.rows())))

    launch = world.launch(FIRST)

    assert isinstance(launch, Launch)
    assert launch.outcome == Outcome("spawn", Status.OK, BACKEND, f"placed-{launch.agent.name}")
    held = seen[0][f"{SLUG}/{FIRST}"]
    assert (held["state"], held["execution_id"], held["generation"]) == (RESERVED, launch.agent.execution_id, 1)
    assert held["expires_ms"] == 1000 + TTL
    assert launch.slot == Slot(ACCOUNT, f"{SLUG}/{FIRST}", launch.agent.execution_id, 1, RESERVED, 1000 + TTL)
    assert launch.agent.account == ACCOUNT
    assert launch.agent.seat == FIRST


def test_the_runtime_request_carries_the_grant_the_execution_and_its_generation(world):
    launch = world.launch(FIRST)

    [request] = world.runtime.requests
    assert request.task == {
        "id": "task",
        "launch_grant": launch.grant,
        "execution_id": launch.agent.execution_id,
        "generation": 1,
    }
    assert (request.lane, request.name) == ("eng", launch.agent.name)
    assert world.grants.verify(SLUG, launch.grant).account == ACCOUNT


def test_reserving_does_not_register_the_grant(world):
    launch = world.launch(FIRST)

    assert world.grants.registration(SLUG, launch.agent.execution_id) is None
    assert world.fleet.records() == []


def test_registration_turns_the_reservation_into_occupancy(world):
    launch = world.launch(FIRST)

    slot = world.launcher.registered(world.session(launch), launch.grant)

    assert slot == Slot(
        ACCOUNT, f"{SLUG}/{FIRST}", launch.agent.execution_id, 1, OCCUPIED, 0, world.session(launch).key()
    )
    assert world.rows()[f"{SLUG}/{FIRST}"]["state"] == OCCUPIED
    assert world.grants.registration(SLUG, launch.agent.execution_id).registered_at == "1970-01-01T00:00:01Z"
    [record] = world.fleet.records()
    assert (record.state, record.seat, record.execution_id) == (LIVE, FIRST, launch.agent.execution_id)


def test_registration_names_its_own_execution_to_the_grant(world):
    launch = world.launch(FIRST)
    other = replace(world.session(launch), generation=2)

    with pytest.raises(GrantRefused) as refused:
        world.launcher.registered(other, launch.grant)

    assert str(refused.value) == "registration generation is outside the launch grant"
    assert world.rows()[f"{SLUG}/{FIRST}"]["state"] == RESERVED


def test_exit_releases_the_slot_after_the_grant_expired(world):
    launch = world.launch(FIRST)
    world.launcher.registered(world.session(launch), launch.grant)
    world.clock[0] += 3_600_000

    released = world.launcher.exited(launch.agent)

    assert released == Slot(
        ACCOUNT, f"{SLUG}/{FIRST}", launch.agent.execution_id, 1, OCCUPIED, 0, world.session(launch).key()
    )
    assert world.rows() == {}
    assert world.capacity.slots(ACCOUNT) == []


def test_exit_frees_the_account_for_the_next_launch(world):
    first = world.launch(FIRST)
    world.launcher.registered(world.session(first), first.grant)
    refused = world.launch(SECOND)
    assert refused.outcome.detail == "account_full"

    world.launcher.exited(first.agent)
    second = world.launch(SECOND, previous=refused.agent.execution_id)

    assert second.outcome.ok
    assert [slot.holder for slot in world.capacity.slots(ACCOUNT)] == [f"{SLUG}/{SECOND}"]


def test_a_second_exit_releases_nothing(world):
    launch = world.launch(FIRST)
    world.launcher.exited(launch.agent)

    assert world.launcher.exited(launch.agent) is None
    assert world.rows() == {}


def test_a_second_simultaneous_launch_on_a_full_account_is_refused(monkeypatch):
    second = []
    world = World(monkeypatch, runtime=Remote(during=lambda _: second.append(world.launch(SECOND))))

    first = world.launch(FIRST)

    assert first.outcome.ok
    [refused] = second
    assert refused.outcome == Outcome("spawn", Status.REFUSED, BACKEND, detail="account_full")
    assert refused.slot is None
    assert [request.name for request in world.runtime.requests] == [first.agent.name]
    assert list(world.rows()) == [f"{SLUG}/{FIRST}"]
    assert world.capacity.account_reservation_conflicts_total() == 1


@pytest.mark.parametrize("status", [Status.REFUSED, Status.UNAVAILABLE, Status.UNSUPPORTED])
def test_a_spawn_that_did_not_launch_releases_its_reservation(monkeypatch, status):
    world = World(monkeypatch, runtime=Remote(status))

    launch = world.launch(FIRST)

    assert launch.outcome.status is status
    assert world.rows() == {}


def test_an_ambiguous_spawn_keeps_its_bounded_reservation(monkeypatch):
    world = World(monkeypatch, runtime=Remote(Status.AMBIGUOUS))

    launch = world.launch(FIRST)

    assert world.rows()[f"{SLUG}/{FIRST}"]["state"] == RESERVED
    world.clock[0] += TTL
    assert world.capacity.slots(ACCOUNT) == []
    assert launch.outcome.status is Status.AMBIGUOUS


def test_a_router_without_the_backend_never_reaches_a_runtime_and_releases(world):
    world.launcher.router = RuntimeRouter([world.runtime], "elsewhere")

    launch = world.launch(FIRST)

    assert launch.outcome == Outcome(
        "spawn", Status.UNAVAILABLE, "elsewhere", detail="no enabled runtime for elsewhere"
    )
    assert world.runtime.requests == []
    assert world.rows() == {}


def test_a_stale_exit_cannot_free_the_slot_its_replacement_holds(world):
    old = world.launch(FIRST)
    replacement = world.launch(FIRST, previous=old.agent.execution_id)
    before = world.rows()

    with pytest.raises(SwarmError) as refused:
        world.launcher.exited(old.agent)

    assert str(refused.value) == "stale_generation"
    assert replacement.agent.generation == 2
    assert world.rows() == before
    assert before[f"{SLUG}/{FIRST}"]["execution_id"] == replacement.agent.execution_id


def test_verify_refuses_a_grant_this_controller_never_issued(world):
    launch = world.launch(FIRST)
    world.store.redis.delete(world.store.key(SLUG, "launch-grants"))

    with pytest.raises(GrantRefused) as refused:
        world.grants.verify(SLUG, launch.grant)

    assert str(refused.value) == "launch grant was not issued by this controller"


def test_verify_refuses_a_revoked_grant(world):
    launch = world.launch(FIRST)
    world.grants.disable(SLUG)

    with pytest.raises(GrantRefused) as refused:
        world.grants.verify(SLUG, launch.grant)

    assert str(refused.value) == "launch grant was revoked"
    assert world.grants.registration(SLUG, launch.agent.execution_id) is None


def test_verify_returns_the_grant_identity_unregistered(world):
    launch = world.launch(FIRST)

    found = world.grants.verify(SLUG, launch.grant)

    assert (found.swarm_id, found.execution_id, found.generation, found.seat_id) == (
        SLUG,
        launch.agent.execution_id,
        1,
        FIRST,
    )
    assert (found.account, found.brain_id, found.project_ids, found.registered_at) == (ACCOUNT, "swarm", [PROJECT], "")
