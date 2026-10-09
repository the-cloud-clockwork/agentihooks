from dataclasses import replace

import pytest

from scripts.swarm import lease
from scripts.swarm.store import AgentRecord, RedisStore, SwarmConfig, SwarmError
from scripts.swarm_v2.controller import Controller
from scripts.swarm_v2.runtime.operations import Observation, OperationRequest, Phase

pytestmark = pytest.mark.unit


class Transport:
    backend = "kubernetes"

    def __init__(self):
        self.effects = {}
        self.creations = 0
        self.lose_ack = False
        self.before_apply = lambda: None

    def observe_operation(self, operation):
        return self.effects.get(operation.operation_id, Observation(Phase.ABSENT))

    def apply_operation(self, operation, payload):
        self.before_apply()
        self.creations += 1
        result = Observation(
            Phase.APPLIED,
            operation.execution_id,
            operation.generation,
            operation.backend,
            operation.payload_digest,
            {"uid": "confirmed"},
        )
        self.effects[operation.operation_id] = result
        if self.lose_ack:
            self.lose_ack = False
            raise TimeoutError("acknowledgement lost")
        return result


@pytest.fixture
def fixture(monkeypatch):
    import fakeredis

    server = fakeredis.FakeServer()
    first = RedisStore(fakeredis.FakeRedis(server=server, decode_responses=True))
    second = RedisStore(fakeredis.FakeRedis(server=server, decode_responses=True))
    first.create(SwarmConfig("fixture", "agentihooks", 1, 0))
    clock = [1000]
    monkeypatch.setattr(lease, "now_ms", lambda store: clock[0])
    transport = Transport()
    grant = {"allowed": True}
    controllers = tuple(
        Controller(store, "fixture", [transport], lambda: grant["allowed"]) for store in (first, second)
    )
    return first, controllers, transport, clock, grant


def agent(store):
    return AgentRecord(
        store.next_name("fixture", "eng"),
        "eng",
        "task",
        seat="eng-1@fixture",
        runtime_backend="kubernetes",
        runtime_target={"pod_namespace": "workers", "pod_name": "attempt"},
    )


def request(attempt):
    return OperationRequest(attempt.execution_id, attempt.generation, "spawn", {"prompt": "fixture"}, "create")


def test_two_controllers_admit_one_generation_and_persist_epochs(fixture):
    store, (first, second), transport, clock, _ = fixture
    assert first.acquire()
    assert not second.acquire()
    attempt = first.admit(agent(store))
    operation = first.execute(request(attempt))
    assert operation.phase is Phase.APPLIED
    assert operation.controller_epoch == 1
    assert first.intents()[0]["controller_epoch"] == 1
    assert first.intents()[0]["execution_id"] == attempt.execution_id
    assert first.controller_leader_changes_total() == 1
    clock[0] += 100
    assert first.renew()
    assert first.controller_leader_changes_total() == 1
    with pytest.raises(SwarmError):
        second.admit(agent(store))
    assert len(store.execution_registry.records("fixture")) == 1
    assert transport.creations == 1


def test_paused_former_leader_and_delayed_renewal_cannot_mutate(fixture):
    store, (first, second), transport, clock, _ = fixture
    assert first.acquire()
    attempt = first.admit(agent(store))
    clock[0] += lease.TTL_MS
    assert second.acquire()
    before = store.redis.hgetall(store.key("fixture", "runtime-operations"))
    assert not first.renew()
    with pytest.raises(SwarmError):
        first.execute(request(attempt))
    with pytest.raises(SwarmError):
        first.admit(replace(agent(store), name=attempt.name), attempt.execution_id)
    assert store.redis.hgetall(store.key("fixture", "runtime-operations")) == before
    assert len(store.execution_registry.records("fixture")) == 1
    assert transport.creations == 0
    assert second.controller_leader_changes_total() == 2


def test_replacement_reconciles_confirmed_effect_before_admission(fixture):
    store, (first, second), transport, clock, _ = fixture
    assert first.acquire()
    attempt = first.admit(agent(store))
    transport.lose_ack = True
    interrupted = first.execute(request(attempt))
    assert interrupted.phase is Phase.UNKNOWN
    clock[0] += lease.TTL_MS
    assert second.acquire()
    assert store.operation_journal.get("fixture", interrupted.operation_id).phase is Phase.APPLIED
    replay = second.execute(request(attempt))
    assert replay.controller_epoch == 1
    assert transport.creations == 1
    assert len(store.operation_journal.records("fixture")) == 1


def test_rollback_disables_admission_and_preserves_history(fixture):
    store, (first, _), transport, clock, grant = fixture
    assert first.acquire()
    attempt = first.admit(agent(store))
    operation = first.execute(request(attempt))
    assert first.release()
    rollback = Controller(store, "fixture", [transport], lambda: grant["allowed"], admission_enabled=False)
    assert rollback.acquire()
    with pytest.raises(SwarmError):
        rollback.admit(agent(store))
    with pytest.raises(SwarmError):
        rollback.execute(request(attempt))
    assert store.operation_journal.get("fixture", operation.operation_id) == operation
    assert len(rollback.intents()) == 1
    assert transport.creations == 1


def test_revoked_grant_blocks_cached_authority(fixture):
    store, (first, second), transport, _, grant = fixture
    grant["allowed"] = False
    with pytest.raises(SwarmError):
        first.acquire()
    assert lease.current(store, "fixture") is None
    grant["allowed"] = True
    assert first.acquire()
    grant["allowed"] = False
    with pytest.raises(SwarmError):
        first.admit(agent(store))
    assert store.execution_registry.records("fixture") == []
    assert transport.creations == 0


@pytest.mark.parametrize("run", range(2))
def test_independent_controllers_race_for_one_admission(fixture, run):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    store, controllers, transport, _, _ = fixture
    barrier = Barrier(2)
    candidate = agent(store)

    def race(controller):
        barrier.wait()
        if controller.acquire():
            return controller.admit(candidate)
        return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        attempts = list(pool.map(race, controllers))
    assert sum(attempt is not None for attempt in attempts) == 1
    assert len(store.execution_registry.records("fixture")) == 1
    assert len(controllers[0].intents()) == 1
    assert controllers[0].controller_leader_changes_total() == 1
    assert transport.creations == 0


def test_failed_reconciliation_cannot_be_bypassed_by_renewal(fixture, monkeypatch):
    store, (first, _), transport, _, _ = fixture

    def fail(slug):
        raise ConnectionError("reconciliation unavailable")

    monkeypatch.setattr(first.operations, "recover", fail)
    with pytest.raises(ConnectionError):
        first.acquire()
    assert not first.renew()
    with pytest.raises(SwarmError):
        first.admit(agent(store))
    assert store.execution_registry.records("fixture") == []
    assert transport.creations == 0


def test_failed_renewal_disables_mutations_even_after_connection_recovers(fixture, monkeypatch):
    store, (first, _), transport, _, _ = fixture
    assert first.acquire()

    def fail(*args):
        raise ConnectionError("renewal unavailable")

    monkeypatch.setattr(lease, "renew", fail)
    with pytest.raises(ConnectionError):
        first.renew()
    with pytest.raises(SwarmError):
        first.admit(agent(store))
    assert transport.creations == 0


def test_takeover_during_journal_transaction_refuses_stale_write(fixture, monkeypatch):
    store, (first, second), transport, clock, _ = fixture
    assert first.acquire()
    attempt = first.admit(agent(store))
    original = store.redis.pipeline
    once = [True]

    def pipeline():
        pipe = original()
        execute = pipe.execute

        def interrupted():
            if once[0]:
                once[0] = False
                clock[0] += lease.TTL_MS
                assert second.acquire()
            return execute()

        pipe.execute = interrupted
        return pipe

    monkeypatch.setattr(store.redis, "pipeline", pipeline)
    with pytest.raises(SwarmError):
        first.execute(request(attempt))
    assert store.operation_journal.records("fixture") == []
    assert transport.creations == 0


def test_takeover_after_observation_prevents_external_apply(fixture, monkeypatch):
    store, (first, second), transport, clock, _ = fixture
    assert first.acquire()
    attempt = first.admit(agent(store))
    observe = transport.observe_operation
    once = [True]

    def interrupted(operation):
        result = observe(operation)
        if once[0]:
            once[0] = False
            clock[0] += lease.TTL_MS
            assert second.acquire()
        return result

    monkeypatch.setattr(transport, "observe_operation", interrupted)
    with pytest.raises(SwarmError):
        first.execute(request(attempt))
    assert transport.creations == 0


@pytest.mark.parametrize("case", ("case_a", "case_b", "case_c"))
def test_package_acceptance_cases(case):
    from tests import sv2_ctl01_cases

    assert getattr(sv2_ctl01_cases, case)()["passed"]
