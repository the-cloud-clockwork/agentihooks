import json
from dataclasses import replace
from pathlib import Path

import pytest
from redis.exceptions import WatchError

import scripts.swarm_v2.registry as registry
from hooks.proc import Process
from scripts.swarm.store import SwarmError
from scripts.swarm_v2.auth_context import GrantRefused
from scripts.swarm_v2.registry import CLOSED, LIVE, SUSPECT, FleetRegistry, Scope, Session
from tests.sv2_ctl02_cases import build

pytestmark = pytest.mark.unit

INPUTS = json.loads((Path(__file__).parent / "fixtures/swarm_v2/fleet-registry.json").read_text())
SLUG = "fixture"
PID = INPUTS["pid"]
MASTER_SEAT = INPUTS["master_seat"]
GONE = {1: Process(1, 0, 1, 1, 5, "S", "init", ("init",))}


def scope(machine):
    found = INPUTS["machines"][machine]
    return Scope(found["backend"], found["host"], found["namespace"])


def session(machine, agent, name=None):
    found = INPUTS["machines"][machine]
    return Session(
        found["session"],
        scope(machine),
        PID,
        found["pid_start"],
        found["harness"],
        agent.name if name is None else name,
        found["conversation"],
        agent.execution_id,
        agent.generation,
    )


def process(start):
    return Process(PID, 1, PID, PID, start, "S", "claude", ("claude",))


class Flaky:
    def __init__(self, redis, failures):
        self.redis, self.failures = redis, failures

    def pipeline(self):
        pipe = self.redis.pipeline()
        real = pipe.execute

        def execute():
            if self.failures:
                self.failures -= 1
                raise WatchError
            return real()

        pipe.execute = execute
        return pipe

    def __getattr__(self, name):
        return getattr(self.redis, name)


class Intruder:
    def __init__(self, redis, write):
        self.redis, self.write = redis, write

    def pipeline(self):
        pipe = self.redis.pipeline()
        real = pipe.execute

        def execute():
            if self.write:
                write, self.write = self.write, None
                write(self.redis)
            return real()

        pipe.execute = execute
        return pipe

    def __getattr__(self, name):
        return getattr(self.redis, name)


class World:
    def __init__(self, monkeypatch):
        self.store, self.authority, _, self.clock, self.start = build(monkeypatch)
        self.fleet = self.registry()
        self.tokens = {}

    def registry(self):
        return FleetRegistry(
            self.store, SLUG, self.authority.authorize, lambda: self.clock[0], INPUTS["stale_after_ms"]
        )

    def rows(self):
        return (
            self.store.redis.hgetall(self.store.key(SLUG, "fleet-sessions")),
            self.store.redis.hgetall(self.store.key(SLUG, "fleet-seats")),
        )

    def states(self):
        return {record.session_id: record.state for record in self.fleet.records()}

    def two_machines(self):
        anton, self.tokens["anton"] = self.start(seat="eng-1@fixture")
        worker, self.tokens["worker"] = self.start(seat="eng-2@fixture")
        self.store.names.alias(INPUTS["alias"], worker.name)
        local = self.fleet.register(session("anton", anton), self.tokens["anton"])
        remote = self.fleet.register(session("worker", worker, INPUTS["alias"]), self.tokens["worker"])
        return local, remote, worker

    def beat(self, machine):
        return self.fleet.heartbeat(scope(machine), INPUTS["machines"][machine]["session"], self.tokens[machine])


@pytest.fixture
def world(monkeypatch):
    return World(monkeypatch)


@pytest.mark.parametrize("run", ["first", "second"])
def test_local_and_remote_sessions_sharing_a_pid_coexist_in_one_registry(world, run):
    assert world.rows() == ({}, {}), run
    local, remote, worker = world.two_machines()

    assert local.key() != remote.key()
    assert len(local.key()) == 32
    assert {(r.scope.backend, r.pid, r.state) for r in world.fleet.records()} == {
        ("local", PID, LIVE),
        ("kubernetes", PID, LIVE),
    }
    assert (remote.name, remote.seat, remote.conversation_id) == (worker.name, "eng-2@fixture", "conv-worker")
    assert world.fleet.find(INPUTS["alias"]) == [remote]
    assert INPUTS["alias"] in world.store.names.aliases(worker.name)
    assert len(world.rows()[0]) == 2
    assert set(world.rows()[1]) == {"eng-1@fixture", "eng-2@fixture"}

    assert world.fleet.observe(scope("anton"), {PID: process(111)}) == 0
    assert world.fleet.observe(scope("anton"), GONE) == 1
    assert world.states() == {"sess-anton": SUSPECT, "sess-worker": LIVE}
    assert world.fleet.observe(scope("anton"), GONE) == 0
    assert world.fleet.fleet_registry_stale_records() == 1


def test_a_pid_reused_with_another_start_time_suspects_only_the_record_of_that_machine(world):
    world.two_machines()

    assert world.fleet.observe(scope("anton"), {PID: process(222)}) == 1
    assert world.states() == {"sess-anton": SUSPECT, "sess-worker": LIVE}


def test_a_remote_observation_judges_only_remote_records(world):
    world.two_machines()

    assert world.fleet.observe(scope("worker"), GONE) == 1
    assert world.states() == {"sess-anton": LIVE, "sess-worker": SUSPECT}


def test_an_unreadable_process_table_judges_nothing(world):
    world.two_machines()
    before = world.rows()

    assert world.fleet.observe(scope("anton"), {}) == 0
    assert world.rows() == before


def test_a_silent_record_turns_suspect_and_is_never_deleted(world):
    world.two_machines()
    world.clock[0] += INPUTS["stale_after_ms"]

    assert world.fleet.sweep() == 0
    world.clock[0] += 1
    assert world.beat("worker").heartbeat_ms == world.clock[0]
    assert world.fleet.sweep() == 1
    assert world.fleet.sweep() == 0
    assert world.states() == {"sess-anton": SUSPECT, "sess-worker": LIVE}
    assert world.fleet.fleet_registry_stale_records() == 1
    assert len(world.rows()[0]) == 2

    assert world.beat("anton").state == LIVE
    assert world.fleet.fleet_registry_stale_records() == 0
    world.clock[0] += INPUTS["stale_after_ms"] + 1
    assert world.fleet.sweep() == 2
    assert world.fleet.observe(scope("anton"), GONE) == 0
    assert world.states() == {"sess-anton": SUSPECT, "sess-worker": SUSPECT}


def test_default_clock_and_stale_window(world):
    fleet = FleetRegistry(world.store, SLUG, world.authority.authorize)
    anton, token = world.start(seat="eng-1@fixture")

    record = fleet.register(replace(session("anton", anton), name="", state=SUSPECT), token)
    assert (record.name, record.state, record.heartbeat_ms) == ("", LIVE, world.clock[0])
    world.clock[0] += registry.STALE_AFTER_MS
    assert fleet.sweep() == 0
    world.clock[0] += 1
    assert fleet.sweep() == 1


def test_a_closed_record_is_not_revived(world):
    local, remote, _ = world.two_machines()

    for machine, record in (("anton", local), ("worker", remote)):
        assert world.fleet.close(scope(machine), record.session_id, world.tokens[machine]).state == CLOSED
        with pytest.raises(SwarmError, match="^session_ended$"):
            world.beat(machine)
    with pytest.raises(SwarmError, match="^session_ended$"):
        world.fleet.register(replace(local, state=LIVE, heartbeat_ms=0), world.tokens["anton"])
    world.clock[0] += INPUTS["stale_after_ms"] + 1
    assert world.fleet.sweep() == 0
    assert world.states() == {"sess-anton": CLOSED, "sess-worker": CLOSED}


def test_replaying_a_suspect_registration_writes_nothing(world):
    local, _, _ = world.two_machines()
    world.fleet.observe(scope("anton"), GONE)
    before = world.rows()

    assert world.fleet.register(replace(local, state=LIVE, heartbeat_ms=0), world.tokens["anton"]).state == SUSPECT
    assert world.rows() == before


def test_heartbeat_and_close_need_the_grant_of_the_record(world):
    world.two_machines()
    before = world.rows()

    with pytest.raises(SwarmError, match="^unknown_session$"):
        world.fleet.heartbeat(scope("anton"), "sess-worker", world.tokens["worker"])
    with pytest.raises(SwarmError, match="^unknown_session$"):
        world.fleet.close(scope("worker"), "sess-anton", world.tokens["anton"])
    for token in ("", world.tokens["anton"]):
        with pytest.raises(SwarmError, match="^forbidden_scope$"):
            world.fleet.heartbeat(scope("worker"), "sess-worker", token)
        with pytest.raises(SwarmError, match="^forbidden_scope$"):
            world.fleet.close(scope("worker"), "sess-worker", token)
    assert world.rows() == before


def test_a_session_without_a_grant_cannot_register(world):
    plain = Session("sess-plain", scope("anton"), PID, 111, "claude")

    for attempt in (plain, replace(plain, state=SUSPECT)):
        with pytest.raises(SwarmError, match="^forbidden_scope$"):
            world.fleet.register(attempt)
    assert world.rows() == ({}, {})


def test_a_superseded_attempt_cannot_beat_its_successor_record(world):
    old, old_token = world.start(seat="eng-1@fixture")
    old_grant = world.authority.authorize(old_token)
    world.fleet.register(session("anton", old), old_token)
    new, new_token = world.start(seat="eng-1@fixture", previous=old.execution_id)
    world.fleet.register(session("anton", new), new_token)
    fenced = FleetRegistry(world.store, SLUG, lambda _: old_grant, lambda: world.clock[0])
    before = world.rows()

    with pytest.raises(GrantRefused, match="^launch grant is for a superseded execution$"):
        world.fleet.heartbeat(scope("anton"), "sess-anton", old_token)
    with pytest.raises(SwarmError, match="^forbidden_scope$"):
        fenced.heartbeat(scope("anton"), "sess-anton", old_token)
    assert world.rows() == before


def test_a_superseded_worker_cannot_beat_beside_its_replacement_but_closes_its_own_record(world):
    old, old_token = world.start(seat="eng-1@fixture")
    old_grant = world.authority.authorize(old_token)
    registered = world.fleet.register(replace(session("anton", old), session_id="sess-old"), old_token)
    new, new_token = world.start(seat="eng-1@fixture", previous=old.execution_id)
    world.fleet.register(replace(session("anton", new), session_id="sess-new"), new_token)
    fenced = FleetRegistry(world.store, SLUG, lambda _: old_grant, lambda: world.clock[0])
    before = world.rows()
    world.clock[0] += 5

    with pytest.raises(SwarmError, match="^stale_generation$"):
        fenced.heartbeat(scope("anton"), "sess-old", old_token)
    assert world.rows() == before

    assert fenced.close(scope("anton"), "sess-old", old_token) == replace(registered, state=CLOSED)
    assert world.states() == {"sess-old": CLOSED, "sess-new": LIVE}
    assert world.fleet.seat("eng-1@fixture")["execution_id"] == new.execution_id
    assert world.fleet.heartbeat(scope("anton"), "sess-new", new_token).heartbeat_ms == world.clock[0]


def test_a_record_replaced_after_its_grant_check_is_not_beaten(world, monkeypatch):
    local, _, _ = world.two_machines()
    newer = replace(local, generation=local.generation + 1)
    sessions = world.store.key(SLUG, "fleet-sessions")
    checked = world.fleet._grant

    def check_then_replace(*args):
        grant = checked(*args)
        world.store.redis.hset(sessions, local.key(), registry.encode(newer))
        return grant

    monkeypatch.setattr(world.fleet, "_grant", check_then_replace)
    world.clock[0] += 5

    with pytest.raises(SwarmError, match="^stale_generation$"):
        world.beat("anton")
    assert [record for record in world.fleet.records() if record.key() == local.key()] == [newer]


def test_local_only_reads_while_distributed_launches_are_disabled(world):
    local, remote, _ = world.two_machines()
    before = world.rows()
    both = sorted([local, remote], key=Session.key)

    assert world.fleet.records(backends={"local"}) == [local]
    assert sorted(world.fleet.records(), key=Session.key) == both
    assert world.fleet.visible({}) == [local]
    assert sorted(world.fleet.visible({"AGENTIHOOKS_RUNTIME_BACKEND": "kubernetes"}), key=Session.key) == both
    disabled = {"AGENTIHOOKS_RUNTIME_BACKEND": "kubernetes", "AGENTIHOOKS_RUNTIME_DISABLED": "kubernetes"}
    assert world.fleet.visible(disabled) == [local]
    assert world.rows() == before


def test_a_remote_session_naming_itself_master_without_a_grant_is_refused(world):
    master_name = world.store.next_name(SLUG, "master")
    worker, token = world.start(seat="eng-2@fixture")
    claimed = session("worker", worker, master_name)
    before = world.rows()

    attempts = [(claimed, ""), (claimed, token), (replace(session("worker", worker), seat=MASTER_SEAT), "")]
    for attempt, grant in attempts:
        with pytest.raises(SwarmError, match="^forbidden_scope$") as refused:
            world.fleet.register(attempt, grant)
        assert token not in str(refused.value)
    assert world.rows() == before
    assert world.fleet.seat(MASTER_SEAT) is None


def test_an_alias_cannot_carry_the_master_name_past_the_grant(world):
    master_name = world.store.next_name(SLUG, "master")
    world.store.names.alias("fixture-master-1", master_name)
    worker, token = world.start(seat="eng-2@fixture")
    before = world.rows()

    with pytest.raises(SwarmError, match="^forbidden_scope$"):
        world.fleet.register(session("worker", worker, "fixture-master-1"), token)
    assert world.rows() == before


def test_a_master_grant_registers_the_master_seat(world):
    master_name = world.store.next_name(SLUG, "master")
    master, token = world.start(seat=MASTER_SEAT)

    record = world.fleet.register(session("worker", master, master_name), token)

    assert (record.name, record.seat) == (master_name, MASTER_SEAT)
    assert world.fleet.seat(MASTER_SEAT) == {
        "execution_id": master.execution_id,
        "generation": master.generation,
        "session": record.key(),
    }


def test_a_grant_for_another_execution_is_refused(world):
    first, first_token = world.start(seat="eng-1@fixture")
    second, _ = world.start(seat="eng-2@fixture")
    before = world.rows()

    with pytest.raises(SwarmError, match="^forbidden_scope$"):
        world.fleet.register(session("worker", second), first_token)
    assert world.rows() == before


@pytest.mark.parametrize("swap", [lambda grant: replace(grant, swarm_id="other"), lambda grant: grant.grant_id])
def test_a_grant_for_another_swarm_or_no_registration_is_refused(world, swap):
    worker, token = world.start(seat="eng-2@fixture")
    grant = world.authority.authorize(token)
    fleet = FleetRegistry(world.store, SLUG, lambda _: swap(grant), lambda: world.clock[0])

    with pytest.raises(SwarmError, match="^forbidden_scope$"):
        fleet.register(session("worker", worker), token)
    assert world.rows() == ({}, {})


def test_an_ungranted_newer_generation_cannot_replace_a_granted_record(world):
    local, _, _ = world.two_machines()
    before = world.rows()

    with pytest.raises(SwarmError, match="^forbidden_scope$"):
        world.fleet.register(replace(local, seat="", generation=local.generation + 1))
    assert world.rows() == before


def test_recovery_from_persisted_state_keeps_aliases_and_generations(world):
    old, old_token = world.start(seat="eng-1@fixture")
    world.store.names.alias(INPUTS["alias"], old.name)
    world.fleet.register(session("worker", old, INPUTS["alias"]), old_token)
    new, new_token = world.start(seat="eng-1@fixture", previous=old.execution_id)
    assert new.generation == old.generation + 1
    current = world.fleet.register(session("worker", new), new_token)
    world.clock[0] += 1

    state = world.store.export(SLUG)
    world.store.redis.flushall()
    world.store.restore(SLUG, state)
    world.fleet = world.registry()

    assert world.fleet.records() == [current]
    assert world.store.names.resolve(INPUTS["alias"]) == old.name
    assert INPUTS["alias"] in world.store.names.aliases(old.name)
    assert world.fleet.seat("eng-1@fixture") == {
        "execution_id": new.execution_id,
        "generation": new.generation,
        "session": current.key(),
    }

    before = world.rows()
    assert world.fleet.register(session("worker", new), new_token) == current
    assert world.rows() == before
    with pytest.raises(SwarmError, match="^forbidden_scope$"):
        world.fleet.register(session("worker", old, INPUTS["alias"]))
    with pytest.raises(GrantRefused, match="^launch grant is for a superseded execution$"):
        world.fleet.register(session("worker", old, INPUTS["alias"]), old_token)
    assert world.rows() == before
    assert world.fleet.find(INPUTS["alias"]) == []
    assert world.fleet.find(new.name) == [current]


def test_an_older_generation_cannot_take_a_seat_a_newer_one_holds(world):
    old, old_token = world.start(seat="eng-1@fixture")
    old_grant = world.authority.authorize(old_token)
    new, new_token = world.start(seat="eng-1@fixture", previous=old.execution_id)
    world.fleet.register(replace(session("anton", new), session_id="sess-new"), new_token)
    fenced = FleetRegistry(world.store, SLUG, lambda _: old_grant, lambda: world.clock[0])
    stale = replace(session("anton", old), session_id="sess-old")
    before = world.rows()

    with pytest.raises(SwarmError, match="^stale_generation$"):
        fenced.register(stale, old_token)
    with pytest.raises(SwarmError, match="^stale_generation$"):
        fenced.register(replace(stale, session_id="sess-new"), old_token)
    with pytest.raises(SwarmError, match="^forbidden_scope$"):
        fenced.register(replace(stale, seat="eng-2@fixture"), old_token)
    assert world.rows() == before


def test_another_execution_at_the_held_generation_cannot_take_the_seat(world):
    anton, token = world.start(seat="eng-1@fixture")
    world.fleet.register(session("anton", anton), token)
    grant = replace(world.authority.authorize(token), execution_id="exe-other")
    fleet = FleetRegistry(world.store, SLUG, lambda _: grant, lambda: world.clock[0])
    before = world.rows()

    with pytest.raises(SwarmError, match="^stale_generation$"):
        fleet.register(replace(session("worker", anton), execution_id="exe-other"), token)
    assert world.rows() == before


def test_a_changed_registration_at_the_same_generation_is_a_conflict(world):
    local, _, _ = world.two_machines()
    before = world.rows()

    with pytest.raises(SwarmError, match="^registration_conflict$"):
        world.fleet.register(replace(local, pid=PID + 1), world.tokens["anton"])
    assert world.rows() == before


def test_a_suspect_record_keeps_its_seat_against_another_grant(world):
    anton, token = world.start(seat="eng-1@fixture")
    world.fleet.register(session("anton", anton), token)
    world.fleet.observe(scope("anton"), GONE)
    grant = replace(world.authority.authorize(token), seat_id="eng-9@fixture")
    fleet = FleetRegistry(world.store, SLUG, lambda _: grant, lambda: world.clock[0])
    before = world.rows()

    with pytest.raises(SwarmError, match="^registration_conflict$"):
        fleet.register(replace(session("anton", anton), pid=PID + 1), token)
    assert world.rows() == before


def test_a_suspect_session_resumed_on_a_new_pid_registers_again(world):
    anton, token = world.start(seat="eng-1@fixture")
    first = world.fleet.register(session("anton", anton), token)
    world.fleet.observe(scope("anton"), GONE)
    world.clock[0] += 1

    resumed = world.fleet.register(replace(session("anton", anton), pid=PID + 1, pid_start=333), token)

    assert (resumed.key(), resumed.pid, resumed.state, resumed.heartbeat_ms) == (first.key(), PID + 1, LIVE, 1001)
    assert world.fleet.records() == [resumed]
    with pytest.raises(SwarmError, match="^registration_conflict$"):
        world.fleet.register(replace(session("anton", anton), pid=PID + 2), token)


def test_writes_retry_a_watch_conflict_and_give_up_after_their_attempts(world, monkeypatch):
    world.two_machines()
    third, third_token = world.start(seat="eng-3@fixture")
    world.authority.authorize(third_token)
    flaky = Flaky(world.store.redis, 1)
    monkeypatch.setattr(world.store, "redis", flaky)

    assert world.fleet.observe(scope("anton"), GONE) == 1
    assert flaky.failures == 0
    world.clock[0] += INPUTS["stale_after_ms"] + 1
    flaky.failures = registry.WRITE_ATTEMPTS
    with pytest.raises(SwarmError, match="^dependency_unavailable$"):
        world.fleet.sweep()
    flaky.failures = registry.WRITE_ATTEMPTS
    with pytest.raises(SwarmError, match="^dependency_unavailable$"):
        world.fleet.register(replace(session("anton", third), session_id="sess-new"), third_token)
    assert flaky.failures == 0
    assert world.states() == {"sess-anton": SUSPECT, "sess-worker": LIVE}


def test_a_seat_taken_meanwhile_fences_the_retried_registration(world, monkeypatch):
    anton, token = world.start(seat="eng-1@fixture")
    world.fleet.register(session("anton", anton), token)
    newer = json.dumps({"execution_id": "exe-newer", "generation": anton.generation + 1, "session": "x"})
    seats = world.store.key(SLUG, "fleet-seats")
    intruder = Intruder(world.store.redis, lambda r: r.hset(seats, "eng-1@fixture", newer))
    monkeypatch.setattr(world.store, "redis", intruder)

    with pytest.raises(SwarmError, match="^stale_generation$"):
        world.fleet.register(replace(session("anton", anton), session_id="sess-two"), token)
    assert world.fleet.seat("eng-1@fixture")["execution_id"] == "exe-newer"


def test_a_seat_taken_meanwhile_fences_the_retried_heartbeat(world, monkeypatch):
    anton, token = world.start(seat="eng-1@fixture")
    registered = world.fleet.register(session("anton", anton), token)
    newer = json.dumps({"execution_id": "exe-newer", "generation": anton.generation + 1, "session": "x"})
    seats = world.store.key(SLUG, "fleet-seats")
    intruder = Intruder(world.store.redis, lambda r: r.hset(seats, "eng-1@fixture", newer))
    monkeypatch.setattr(world.store, "redis", intruder)
    world.clock[0] += 5

    with pytest.raises(SwarmError, match="^stale_generation$"):
        world.beat("anton")
    assert world.fleet.records() == [registered]


def test_a_session_written_meanwhile_fences_the_retried_registration(world, monkeypatch):
    anton, token = world.start(seat="eng-1@fixture")
    world.authority.authorize(token)
    mine = session("anton", anton)
    newer = replace(mine, generation=anton.generation + 4)
    sessions = world.store.key(SLUG, "fleet-sessions")
    intruder = Intruder(world.store.redis, lambda r: r.hset(sessions, mine.key(), registry.encode(newer)))
    monkeypatch.setattr(world.store, "redis", intruder)

    with pytest.raises(SwarmError, match="^stale_generation$"):
        world.fleet.register(mine, token)
    assert world.fleet.records() == [newer]


def test_a_record_round_trips_through_its_stored_document():
    record = Session("s", Scope("kubernetes", "pod", "ns"), 7, 9, "codex", "n", "c", "exe-1", 3, "eng-1@x", SUSPECT, 5)

    assert registry.decode(registry.encode(record)) == record
    assert json.loads(registry.encode(record))["scope"] == {"backend": "kubernetes", "host": "pod", "namespace": "ns"}
    assert record.key() == replace(record, pid=8, state=LIVE).key()
    assert record.key() != replace(record, scope=Scope("kubernetes", "pod", "other")).key()
    assert record.key() != replace(record, session_id="t").key()
    assert record.key() == registry.session_key(Scope("kubernetes", "pod", "ns"), "s")
    assert len(record.key()) == 32
