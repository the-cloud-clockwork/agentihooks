import json
import subprocess
import sys
from dataclasses import replace
from functools import partial

import pytest

from scripts.swarm.keyspace import ROOT
from scripts.swarm.store import MASTER, AgentRecord, RedisStore, SwarmConfig, SwarmError
from scripts.swarm.tick import Placed, SpawnError
from scripts.swarm_v2 import broadcast_bridge
from scripts.swarm_v2 import launch as launch_module
from scripts.swarm_v2.accounts import OCCUPIED, RESERVED, AccountCapacity, Slot
from scripts.swarm_v2.auth_context import GrantRefused, LaunchAuthority, LaunchKey
from scripts.swarm_v2.controller import Controller
from scripts.swarm_v2.hand import HANDED, Hand
from scripts.swarm_v2.launch import HANDS, DistributedLaunch, Launch, LaunchTerms, WorkerHomeCommand
from scripts.swarm_v2.registry import CLOSED, LIVE, FleetRegistry, Scope, Session
from scripts.swarm_v2.runtime.base import Capability, Outcome, Placement, RuntimeRouter, SpawnRequest, Status
from scripts.swarm_v2.runtime.routed import routed

pytestmark = [pytest.mark.unit, pytest.mark.xdist_group("fakeredis")]

SLUG = "fixture"
ACCOUNT = "fixture"
BACKEND = "kubernetes"
TTL = 30000
PROJECT = "github.com/the-cloud-clockwork/agentihooks"
FIRST, SECOND = "eng-1@fixture", "eng-2@fixture"
API = "http://swarm-api.agentihooks-swarm.svc:8780"
OTHER_API = "https://swarm.example.test:8443"
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


class Homes:
    def __init__(self, hand=HANDED):
        self.hand_result, self.calls = hand, []

    def hand(self, agent, grant):
        self.calls.append((agent.execution_id, grant))
        return self.hand_result


class World:
    def __init__(self, monkeypatch, cap=1, runtime=None, homes=None):
        import fakeredis

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
        self.homes = homes or Homes()
        self.launcher = DistributedLaunch(
            self.controller, self.grants, self.capacity, self.fleet, self.router, self.homes
        )
        self.terms = LaunchTerms(ACCOUNT, cap, TTL, (PROJECT,), "swarm", API)

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

    def hands(self):
        return {key: json.loads(raw) for key, raw in self.store.redis.hgetall(self.store.key(SLUG, HANDS)).items()}


@pytest.fixture
def world(monkeypatch):
    return World(monkeypatch)


def pod_target(request):
    return {"pod_namespace": "workers", "pod_name": f"swarm-{request.task['id']}"}


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
        "endpoints": {"AGENTIHOOKS_SWARM_API_URL": API},
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
    assert world.grants.verify(SLUG, refused.grant).execution_id == refused.agent.execution_id
    assert [request.name for request in world.runtime.requests] == [first.agent.name]
    assert list(world.rows()) == [f"{SLUG}/{FIRST}"]
    assert world.capacity.account_reservation_conflicts_total() == 1


def test_a_refused_spawn_releases_even_after_its_grant_was_revoked(monkeypatch):
    world = World(monkeypatch, runtime=Remote(Status.REFUSED, during=lambda _: world.grants.disable(SLUG)))

    launch = world.launch(FIRST)

    assert launch.outcome.status is Status.REFUSED
    assert world.rows() == {}


def test_exit_of_the_stored_execution_releases_its_slot(world):
    launch = world.launch(FIRST)
    stored = world.store.execution(SLUG, launch.agent.execution_id)

    assert stored.account == ACCOUNT
    assert world.launcher.exited(stored).holder == f"{SLUG}/{FIRST}"
    assert world.rows() == {}


def test_a_registration_after_the_reservation_expired_closes_its_session(world):
    launch = world.launch(FIRST)
    world.clock[0] += TTL

    with pytest.raises(SwarmError) as refused:
        world.launcher.registered(world.session(launch), launch.grant)

    assert str(refused.value) == "reservation_expired"
    [record] = world.fleet.records()
    assert record.state == CLOSED
    assert world.capacity.slots(ACCOUNT) == []


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


@pytest.mark.parametrize("change", [{"generation": 2}, {"execution_id": "exe-other"}])
def test_an_exit_must_match_both_the_execution_and_the_generation(world, change):
    launch = world.launch(FIRST)
    before = world.rows()

    with pytest.raises(SwarmError) as refused:
        world.launcher.exited(replace(launch.agent, **change))

    assert str(refused.value) == "stale_generation"
    assert world.rows() == before


def test_verify_refuses_a_grant_this_controller_never_issued(world):
    launch = world.launch(FIRST)
    world.store.redis.delete(world.store.key(SLUG, "launch-grants"))

    with pytest.raises(GrantRefused) as refused:
        world.grants.verify(SLUG, launch.grant)

    assert str(refused.value) == "launch grant was not issued by this controller"
    assert world.grants.registration(SLUG, launch.agent.execution_id) is None
    assert world.fleet.records() == []


def test_verify_refuses_a_revoked_grant(world):
    launch = world.launch(FIRST)
    world.grants.disable(SLUG)

    with pytest.raises(GrantRefused) as refused:
        world.grants.verify(SLUG, launch.grant)

    assert str(refused.value) == "launch grant was revoked"
    assert world.grants.registration(SLUG, launch.agent.execution_id) is None
    assert world.fleet.records() == []


def test_a_superseded_worker_cannot_register_again_but_can_close_its_session(world):
    old = world.launch(FIRST)
    world.launcher.registered(world.session(old), old.grant)
    world.launch(FIRST, previous=old.agent.execution_id)
    body = {"execution_id": old.agent.execution_id, "generation": 1}

    with pytest.raises(GrantRefused) as refused:
        world.grants.register(SLUG, old.grant, body)
    closed = world.fleet.close(MACHINE, world.session(old).session_id, old.grant)

    assert str(refused.value) == "launch grant is for a superseded execution"
    assert closed.state == CLOSED


def test_an_occupy_refused_after_a_replacement_reserved_closes_the_session(world, monkeypatch):
    old = world.launch(FIRST)
    register = world.fleet.register

    def then_replaced(session, grant):
        record = register(session, grant)
        world.launch(FIRST, previous=old.agent.execution_id)
        return record

    monkeypatch.setattr(world.fleet, "register", then_replaced)

    with pytest.raises(SwarmError) as refused:
        world.launcher.registered(world.session(old), old.grant)

    assert str(refused.value) == "stale_generation"
    [record] = world.fleet.records()
    assert (record.state, record.execution_id) == (CLOSED, old.agent.execution_id)
    assert world.rows()[f"{SLUG}/{FIRST}"]["generation"] == 2


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


def bootstrapped(root):
    def make(request):
        (root / request.task["execution_id"] / "run").mkdir(parents=True)

    return make


def test_a_launch_writes_its_grant_through_the_worker_home_grant_command_without_printing_it(
    monkeypatch, tmp_path, capfd
):
    world = World(monkeypatch, runtime=Remote(during=bootstrapped(tmp_path)), homes=WorkerHomeCommand(tmp_path))

    launch = world.launch(FIRST)

    assert launch.hand == Hand(True, "handed", False)
    attempt = tmp_path / launch.agent.execution_id
    assert broadcast_bridge.grant_path(attempt).read_text() == launch.grant
    assert capfd.readouterr() == ("", "")


def test_a_failed_grant_command_reports_unhanded_and_prints_nothing(monkeypatch, tmp_path, capfd):
    world = World(monkeypatch, homes=WorkerHomeCommand(tmp_path))

    launch = world.launch(FIRST)

    assert launch.outcome == Outcome(
        "spawn", Status.REFUSED, BACKEND, None, "launch grant not handed: grant_command_failed"
    )
    assert launch.hand == Hand(False, "grant_command_failed", False)
    assert not (tmp_path / launch.agent.execution_id).exists()
    assert capfd.readouterr() == ("", "")


def test_the_grant_goes_on_standard_input_never_in_the_command(monkeypatch, tmp_path):
    seen = []

    def run(command, **options):
        seen.append((command, options))
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(launch_module.subprocess, "run", run)
    world = World(monkeypatch, homes=WorkerHomeCommand(tmp_path))

    launch = world.launch(FIRST)

    [(command, options)] = seen
    assert command == [
        sys.executable,
        "-m",
        "scripts.swarm_v2.worker_home",
        "grant",
        str(tmp_path / launch.agent.execution_id),
    ]
    assert options == {"input": launch.grant, "capture_output": True, "text": True, "timeout": 30}
    assert launch.hand == HANDED


@pytest.mark.parametrize(("failure", "reason"), [(1, "grant_command_failed"), (None, "grant_command_unavailable")])
def test_a_grant_command_that_fails_or_times_out_reports_unhanded(monkeypatch, tmp_path, failure, reason):
    def run(command, **options):
        if failure is None:
            raise subprocess.TimeoutExpired(command, options["timeout"])
        return subprocess.CompletedProcess(command, failure)

    monkeypatch.setattr(launch_module.subprocess, "run", run)
    world = World(monkeypatch, homes=WorkerHomeCommand(tmp_path))

    assert world.launch(FIRST).hand == Hand(False, reason, False)


def test_a_grant_command_that_cannot_start_ends_the_launch_and_frees_the_slot(monkeypatch, tmp_path):
    monkeypatch.setattr(launch_module.sys, "executable", str(tmp_path / "missing-python"))
    world = World(monkeypatch, homes=WorkerHomeCommand(tmp_path))

    launch = world.launch(FIRST)

    assert launch.outcome == Outcome(
        "spawn", Status.REFUSED, BACKEND, None, "launch grant not handed: grant_command_unavailable"
    )
    assert launch.hand == Hand(False, "grant_command_unavailable", False)
    assert world.rows() == {}


def test_a_launch_never_shows_its_grant_when_printed(world):
    launch = world.launch(FIRST)

    assert launch.grant not in repr(launch)
    assert launch.agent.execution_id in repr(launch)


def test_a_launched_worker_is_handed_its_own_grant(world):
    launch = world.launch(FIRST)

    assert world.homes.calls == [(launch.agent.execution_id, launch.grant)]
    assert launch.hand == HANDED
    assert launch.outcome == Outcome("spawn", Status.OK, BACKEND, f"placed-{launch.agent.name}")


def test_the_launch_reports_what_the_worker_home_answered(monkeypatch):
    world = World(monkeypatch, homes=Homes(Hand(False, "pod_missing", False)))

    assert world.launch(FIRST).hand == Hand(False, "pod_missing", False)


@pytest.mark.parametrize(
    "hand", [Hand(False, "pod_missing", False), Hand(False, "config_map_refused", True)], ids=["missing", "refused"]
)
def test_a_launch_whose_grant_is_not_handed_ends_and_names_the_reason(monkeypatch, hand):
    world = World(monkeypatch, homes=Homes(hand))

    launch = world.launch(FIRST)

    assert launch.outcome == Outcome("spawn", Status.REFUSED, BACKEND, None, f"launch grant not handed: {hand.reason}")
    assert launch.hand == hand
    assert world.rows() == {}
    world.homes.hand_result = HANDED
    assert world.launch(SECOND).outcome.ok


@pytest.mark.parametrize(
    "hand",
    [HANDED, Hand(False, "pod_missing", False), Hand(False, "config_map_refused", True)],
    ids=["handed", "missing", "refused"],
)
def test_every_hand_is_recorded_under_its_execution(monkeypatch, hand):
    world = World(monkeypatch, homes=Homes(hand))

    launch = world.launch(FIRST)

    assert world.hands() == {
        launch.agent.execution_id: {"handed": hand.handed, "reason": hand.reason, "removed": hand.removed}
    }


@pytest.mark.parametrize("status", [Status.REFUSED, Status.UNAVAILABLE, Status.UNSUPPORTED])
def test_a_spawn_that_did_not_launch_hands_no_grant(monkeypatch, status):
    world = World(monkeypatch, runtime=Remote(status))

    launch = world.launch(FIRST)

    assert world.homes.calls == []
    assert launch.hand is None
    assert world.hands() == {}
    assert launch.agent.seat == FIRST
    assert world.grants.verify(SLUG, launch.grant).execution_id == launch.agent.execution_id
    assert launch.slot == Slot(ACCOUNT, f"{SLUG}/{FIRST}", launch.agent.execution_id, 1, RESERVED, 1000 + TTL)


def test_an_ambiguous_spawn_still_hands_its_grant(monkeypatch):
    world = World(monkeypatch, runtime=Remote(Status.AMBIGUOUS))

    launch = world.launch(FIRST)

    assert world.homes.calls == [(launch.agent.execution_id, launch.grant)]
    assert launch.hand == HANDED
    assert launch.outcome.status is Status.AMBIGUOUS


def test_an_ambiguous_spawn_whose_grant_is_not_handed_stays_ambiguous_and_keeps_its_slot(monkeypatch):
    world = World(monkeypatch, runtime=Remote(Status.AMBIGUOUS), homes=Homes(Hand(False, "pod_missing", False)))

    launch = world.launch(FIRST)

    assert launch.outcome == Outcome("spawn", Status.AMBIGUOUS, BACKEND, None)
    assert launch.hand == Hand(False, "pod_missing", False)
    assert world.rows()[f"{SLUG}/{FIRST}"]["state"] == RESERVED
    assert world.hands() == {launch.agent.execution_id: {"handed": False, "reason": "pod_missing", "removed": False}}
    assert world.grants.verify(SLUG, launch.grant).execution_id == launch.agent.execution_id


def test_a_pod_left_behind_by_an_unhanded_launch_cannot_register_its_withdrawn_grant(monkeypatch):
    world = World(monkeypatch, homes=Homes(Hand(False, "config_map_unavailable", False)))
    launch = world.launch(FIRST)

    with pytest.raises(GrantRefused) as refused:
        world.launcher.registered(world.session(launch), launch.grant)

    assert str(refused.value) == "launch grant was revoked"
    assert world.grants.registration(SLUG, launch.agent.execution_id) is None
    assert world.fleet.records() == []
    assert world.capacity.slots(ACCOUNT) == []


def test_withdrawing_a_grant_revokes_it_once(world):
    launch = world.launch(FIRST)

    assert world.grants.withdraw(SLUG, launch.grant) is True
    assert world.grants.withdraw(SLUG, launch.grant) is False
    with pytest.raises(GrantRefused) as refused:
        world.grants.verify(SLUG, launch.grant)
    assert str(refused.value) == "launch grant was revoked"


def test_a_refused_withdraw_still_frees_the_slot_of_an_unhanded_launch(monkeypatch):
    world = World(monkeypatch, homes=Homes(Hand(False, "pod_missing", False)))

    def refuse(slug, token):
        raise GrantRefused("dependency_unavailable", "launch grants kept changing; the grant was not withdrawn")

    monkeypatch.setattr(world.grants, "withdraw", refuse)

    with pytest.raises(GrantRefused):
        world.launch(FIRST)

    assert world.rows() == {}


def test_a_launch_refused_on_a_full_account_hands_no_grant(world):
    world.launch(FIRST)

    refused = world.launch(SECOND)

    assert refused.outcome.detail == "account_full"
    assert refused.hand is None
    assert len(world.homes.calls) == 1


def test_a_refused_launch_names_the_backend_its_spawn_was_placed_on(monkeypatch):
    world = World(monkeypatch, cap=0)
    world.launcher.router = RuntimeRouter([world.runtime], placement=Placement(BACKEND))

    launch = world.launch(FIRST)

    assert launch.outcome == Outcome("spawn", Status.REFUSED, BACKEND, None, "account_full")


def test_a_tick_spawn_hands_the_worker_the_api_address_of_its_swarm_config(world):
    name = world.store.next_name(SLUG, "eng")
    config = SwarmConfig(SLUG, "agentihooks", 2, 0, api_url=OTHER_API)
    request = SpawnRequest(config, "eng", name, {"id": "task", "seat": FIRST})

    outcome = world.launcher.from_tick(request, replace(world.terms, api_url=""), pod_target)

    assert outcome == Outcome("spawn", Status.OK, BACKEND, f"placed-{name}")
    [sent] = world.runtime.requests
    assert sent.task["endpoints"] == {broadcast_bridge.API_URL: OTHER_API}
    agent = world.store.execution(SLUG, sent.task["execution_id"])
    assert (agent.name, agent.lane, agent.task, agent.seat, agent.account) == (name, "eng", "task", FIRST, ACCOUNT)
    assert (agent.runtime_backend, agent.runtime_target) == (BACKEND, pod_target(request))


def test_a_tick_spawn_without_a_swarm_api_address_is_refused_before_admission(world):
    request = SpawnRequest(SwarmConfig(SLUG, "agentihooks", 2, 0), "eng", "e1", {"id": "task", "seat": FIRST})
    world.launcher.router = RuntimeRouter([world.runtime], placement=Placement(BACKEND))

    outcome = world.launcher.from_tick(request, world.terms, pod_target)

    assert outcome == Outcome("spawn", Status.REFUSED, BACKEND, None, "the swarm config has no API address")
    assert world.runtime.requests == []
    assert world.rows() == {}


def test_the_swarm_config_keeps_its_api_address(world):
    assert world.store.config(SLUG).api_url == ""
    world.store.update(SLUG, api_url=OTHER_API)
    assert world.store.config(SLUG).api_url == OTHER_API


class Herdr:
    def __init__(self):
        self.spawned = []

    def spawn(self, config, lane, name, task):
        self.spawned.append(name)
        return Placed(f"pane-{name}", "claude")


def test_the_tick_runtime_launches_placed_spawns_with_the_swarm_api_address(world):
    herdr, name = Herdr(), world.store.next_name(SLUG, "eng")
    runtime = routed(
        {},
        herdr,
        kubernetes=world.runtime,
        launch=partial(world.launcher.from_tick, terms=world.terms, target=pod_target),
    )
    config = SwarmConfig(SLUG, "agentihooks", 2, 0, api_url=OTHER_API)

    assert runtime.spawn(config, "eng", name, {"id": "task", "seat": FIRST}) == f"placed-{name}"
    assert runtime.spawn(config, MASTER, "m1", {"id": "task"}) == Placed("pane-m1", "claude")

    [sent] = world.runtime.requests
    assert (sent.name, sent.task["endpoints"]) == (name, {broadcast_bridge.API_URL: OTHER_API})
    assert herdr.spawned == ["m1"]


def test_a_tick_spawn_whose_grant_is_not_handed_fails_with_the_reason_and_records_it(monkeypatch):
    world = World(monkeypatch, homes=Homes(Hand(False, "config_map_refused", True)))
    name = world.store.next_name(SLUG, "eng")
    runtime = routed(
        {},
        Herdr(),
        kubernetes=world.runtime,
        launch=partial(world.launcher.from_tick, terms=world.terms, target=pod_target),
    )
    config = SwarmConfig(SLUG, "agentihooks", 2, 0, api_url=OTHER_API)

    with pytest.raises(SpawnError) as failed:
        runtime.spawn(config, "eng", name, {"id": "task", "seat": FIRST})

    assert str(failed.value) == "launch grant not handed: config_map_refused"
    assert failed.value.status == Status.REFUSED
    [sent] = world.runtime.requests
    assert world.hands() == {
        sent.task["execution_id"]: {"handed": False, "reason": "config_map_refused", "removed": True}
    }
    assert world.rows() == {}
