import json
from dataclasses import replace
from pathlib import Path

import pytest

import scripts.swarm_v2.registry as registry
from hooks.proc import Process
from scripts.swarm.store import SwarmError
from scripts.swarm_v2.registry import CLOSED, EXITED, LIVE, SUSPECT, FleetRegistry, Scope, Session
from tests.sv2_ctl02_cases import build

pytestmark = pytest.mark.unit

INPUTS = json.loads((Path(__file__).parent / "fixtures/swarm_v2/fleet-registry.json").read_text())
SLUG = "fixture"
PID = INPUTS["pid"]
MASTER_SEAT = INPUTS["master_seat"]


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


class World:
    def __init__(self, monkeypatch):
        self.store, self.authority, _, self.clock, self.start = build(monkeypatch)
        self.fleet = self.registry()

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
        anton, anton_token = self.start(seat="eng-1@fixture")
        worker, worker_token = self.start(seat="eng-2@fixture")
        self.store.names.alias(INPUTS["alias"], worker.name)
        local = self.fleet.register(session("anton", anton), anton_token)
        remote = self.fleet.register(session("worker", worker, INPUTS["alias"]), worker_token)
        return local, remote, worker


@pytest.fixture
def world(monkeypatch):
    return World(monkeypatch)


@pytest.mark.parametrize("run", ["first", "second"])
def test_local_and_remote_sessions_sharing_a_pid_coexist_in_one_registry(world, run):
    local, remote, worker = world.two_machines()

    assert local.key() != remote.key()
    assert {(r.scope.backend, r.pid, r.state) for r in world.fleet.records()} == {
        ("local", PID, LIVE),
        ("kubernetes", PID, LIVE),
    }
    assert (remote.name, remote.seat, remote.conversation_id) == (worker.name, "eng-2@fixture", "conv-worker")
    assert world.fleet.find(INPUTS["alias"]) == [remote]
    assert INPUTS["alias"] in world.store.names.aliases(worker.name)
    assert len(world.rows()[0]) == 2

    assert world.fleet.observe(scope("anton"), {PID: process(111)}) == 0
    assert world.fleet.observe(scope("anton"), {}) == 1
    assert world.states() == {"sess-anton": EXITED, "sess-worker": LIVE}
    assert world.fleet.observe(scope("anton"), {}) == 0
    assert world.fleet.fleet_registry_stale_records() == 0


def test_a_pid_reused_with_another_start_time_exits_only_the_record_of_that_machine(world):
    world.two_machines()

    assert world.fleet.observe(scope("anton"), {PID: process(222)}) == 1
    assert world.states() == {"sess-anton": EXITED, "sess-worker": LIVE}


def test_a_remote_observation_judges_only_remote_records(world):
    world.two_machines()

    assert world.fleet.observe(scope("worker"), {}) == 1
    assert world.states() == {"sess-anton": LIVE, "sess-worker": EXITED}


def test_a_silent_record_turns_suspect_and_is_never_deleted(world):
    world.two_machines()
    world.clock[0] += INPUTS["stale_after_ms"]

    assert world.fleet.sweep() == 0
    world.clock[0] += 1
    assert world.fleet.heartbeat(scope("worker"), "sess-worker").heartbeat_ms == world.clock[0]
    assert world.fleet.sweep() == 1
    assert world.fleet.sweep() == 0
    assert world.states() == {"sess-anton": SUSPECT, "sess-worker": LIVE}
    assert world.fleet.fleet_registry_stale_records() == 1
    assert len(world.rows()[0]) == 2

    assert world.fleet.heartbeat(scope("anton"), "sess-anton").state == LIVE
    assert world.fleet.fleet_registry_stale_records() == 0


def test_an_exited_or_closed_record_is_not_revived_by_a_heartbeat(world):
    world.two_machines()
    world.fleet.observe(scope("anton"), {})
    assert world.fleet.close(scope("worker"), "sess-worker").state == CLOSED

    for machine in ("anton", "worker"):
        with pytest.raises(SwarmError, match="^session_ended$"):
            world.fleet.heartbeat(scope(machine), INPUTS["machines"][machine]["session"])
    world.clock[0] += INPUTS["stale_after_ms"] + 1
    assert world.fleet.sweep() == 0
    assert world.states() == {"sess-anton": EXITED, "sess-worker": CLOSED}


def test_a_heartbeat_from_another_scope_finds_no_session(world):
    world.two_machines()
    before = world.rows()

    with pytest.raises(SwarmError, match="^unknown_session$"):
        world.fleet.heartbeat(scope("anton"), "sess-worker")
    with pytest.raises(SwarmError, match="^unknown_session$"):
        world.fleet.close(scope("worker"), "sess-anton")
    assert world.rows() == before


def test_local_only_reads_leave_remote_records_in_place(world):
    local, remote, _ = world.two_machines()

    assert world.fleet.records(backends={"local"}) == [local]
    assert sorted(world.fleet.records(), key=Session.key) == sorted([local, remote], key=Session.key)


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


def test_a_grant_for_another_swarm_is_refused(world):
    worker, token = world.start(seat="eng-2@fixture")
    grant = world.authority.authorize(token)
    fleet = FleetRegistry(world.store, SLUG, lambda _: replace(grant, swarm_id="other"), lambda: world.clock[0])

    with pytest.raises(SwarmError, match="^forbidden_scope$"):
        fleet.register(session("worker", worker), token)
    assert world.rows() == (dict(), dict())


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
    with pytest.raises(SwarmError, match="^stale_generation$"):
        world.fleet.register(session("worker", old, INPUTS["alias"]))
    with pytest.raises(SwarmError):
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
    with pytest.raises(SwarmError, match="^forbidden_scope$"):
        fenced.register(replace(stale, seat="eng-2@fixture"), old_token)
    assert world.rows() == before


def test_a_changed_registration_at_the_same_generation_is_a_conflict(world):
    local, _, _ = world.two_machines()
    before = world.rows()

    with pytest.raises(SwarmError, match="^registration_conflict$"):
        world.fleet.register(replace(local, pid=PID + 1, seat=""))
    assert world.rows() == before


def test_an_exited_session_resumed_on_a_new_pid_registers_again(world):
    anton, token = world.start(seat="eng-1@fixture")
    first = world.fleet.register(session("anton", anton), token)
    world.fleet.observe(scope("anton"), {})
    world.clock[0] += 1

    resumed = world.fleet.register(replace(session("anton", anton), pid=PID + 1, pid_start=333), token)

    assert (resumed.key(), resumed.pid, resumed.state, resumed.heartbeat_ms) == (first.key(), PID + 1, LIVE, 1001)
    assert world.fleet.records() == [resumed]
    with pytest.raises(SwarmError, match="^registration_conflict$"):
        world.fleet.register(replace(session("anton", anton), pid=PID + 2))


def test_a_record_round_trips_through_its_stored_document():
    record = Session("s", Scope("kubernetes", "pod", "ns"), 7, 9, "codex", "n", "c", "exe-1", 3, "eng-1@x", SUSPECT, 5)

    assert registry.decode(registry.encode(record)) == record
    assert json.loads(registry.encode(record))["scope"] == {"backend": "kubernetes", "host": "pod", "namespace": "ns"}
    assert record.key() == replace(record, pid=8, state=LIVE).key()
    assert record.key() != replace(record, scope=Scope("kubernetes", "pod", "other")).key()
    assert record.key() != replace(record, session_id="t").key()
