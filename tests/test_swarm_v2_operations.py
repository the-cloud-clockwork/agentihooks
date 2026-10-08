from dataclasses import replace

import pytest

from scripts.swarm.store import AgentRecord, RedisStore, SwarmConfig
from scripts.swarm_v2.runtime.operations import (
    Observation,
    OperationConflict,
    OperationRequest,
    Operations,
    Phase,
)

pytestmark = [pytest.mark.unit, pytest.mark.xdist_group("fakeredis")]


class LostAcknowledgement:
    backend = "kubernetes"

    def __init__(self):
        self.effects = {}
        self.creations = 0
        self.lookups = []
        self.lose_ack = True

    def observe_operation(self, operation):
        self.lookups.append(operation.execution_id)
        return self.effects.get(operation.operation_id, Observation(Phase.ABSENT))

    def apply_operation(self, operation, payload):
        if operation.operation_id not in self.effects:
            self.creations += 1
            self.effects[operation.operation_id] = Observation(
                Phase.APPLIED,
                operation.execution_id,
                operation.generation,
                operation.backend,
                operation.payload_digest,
                {"uid": f"object-{self.creations}"},
            )
        if self.lose_ack:
            self.lose_ack = False
            raise TimeoutError("acknowledgement lost")
        return self.effects[operation.operation_id]


@pytest.fixture
def fixture():
    import fakeredis

    store = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    store.create(SwarmConfig("fixture", "agentihooks", 1, 0))
    agent = store.start_execution(
        "fixture",
        AgentRecord(
            store.next_name("fixture", "eng"),
            "eng",
            "task",
            seat="eng-1@fixture",
            runtime_backend="kubernetes",
            runtime_target={"pod_namespace": "workers", "pod_name": "attempt"},
        ),
    )
    transport = LostAcknowledgement()
    return store, agent, transport, Operations(store, [transport])


def request(agent, action="spawn", payload=None, key="creation"):
    return OperationRequest(agent.execution_id, agent.generation, action, payload or {"prompt": "fixture"}, key)


@pytest.mark.parametrize("independent", range(2))
def test_create_loses_acknowledgement_and_retry_observes_single_object(fixture, independent):
    store, agent, transport, operations = fixture
    first = operations.execute("fixture", request(agent))
    assert first.phase is Phase.UNKNOWN
    assert first.operation_id
    assert store.operation_journal.get("fixture", first.operation_id) == first
    assert operations.runtime_ambiguous_operations("fixture") == 1
    retried = operations.execute("fixture", request(agent))
    assert retried.phase is Phase.APPLIED
    assert retried.operation_id == first.operation_id
    assert retried.result == {"uid": "object-1"}
    assert transport.creations == 1
    assert transport.lookups == [agent.execution_id, agent.execution_id]
    assert operations.runtime_ambiguous_operations("fixture") == 0


def test_changed_prompt_conflicts_without_any_mutation(fixture):
    store, agent, transport, operations = fixture
    operations.execute("fixture", request(agent, "command", {"prompt": "original"}, "command"))
    before = store.redis.hgetall(store.key("fixture", "runtime-operations"))
    with pytest.raises(OperationConflict, match="payload"):
        operations.execute("fixture", request(agent, "command", {"prompt": "changed"}, "command"))
    assert store.redis.hgetall(store.key("fixture", "runtime-operations")) == before
    assert transport.creations == 1


@pytest.mark.parametrize("independent", range(2))
def test_restart_recovers_unresolved_operation_by_observation(fixture, independent):
    store, agent, transport, operations = fixture
    first = operations.execute("fixture", request(agent))
    restarted = Operations(RedisStore(store.redis), [transport], dispatch_enabled=False)
    recovered = restarted.recover("fixture")
    assert recovered == [replace(first, phase=Phase.APPLIED, result={"uid": "object-1"})]
    assert restarted.execute("fixture", request(agent)) == recovered[0]
    assert transport.creations == 1
    assert not restarted.unresolved("fixture")


def test_stale_generation_is_refused_before_dispatch(fixture):
    store, agent, transport, operations = fixture
    store.start_execution(
        "fixture",
        replace(agent, execution_id="", generation=0),
        agent.execution_id,
    )
    before = store.redis.hgetall(store.key("fixture", "runtime-operations"))
    with pytest.raises(OperationConflict, match="stale"):
        operations.execute("fixture", request(agent))
    assert store.redis.hgetall(store.key("fixture", "runtime-operations")) == before
    assert not transport.lookups
    assert transport.creations == 0


def test_unknown_observation_never_authorizes_another_mutation(fixture):
    store, agent, transport, operations = fixture
    first = operations.execute("fixture", request(agent))
    transport.effects.clear()
    transport.observe_operation = lambda operation: Observation(Phase.UNKNOWN)
    retried = operations.execute("fixture", request(agent))
    assert retried == first
    assert transport.creations == 1
    assert store.operation_journal.get("fixture", first.operation_id) == first


@pytest.mark.parametrize(
    "field,value",
    [
        ("execution_id", "foreign"),
        ("generation", 99),
        ("backend", "local"),
        ("payload_digest", "changed"),
    ],
)
def test_foreign_observation_is_refused_without_local_fallback(fixture, field, value):
    store, agent, transport, operations = fixture
    first = operations.execute("fixture", request(agent))
    transport.effects[first.operation_id] = replace(transport.effects[first.operation_id], **{field: value})
    rejected = operations.execute("fixture", request(agent))
    assert rejected.phase is Phase.REFUSED
    assert rejected.result == {}
    assert transport.creations == 1


def test_disabled_dispatch_preserves_accepted_journal_and_never_creates(fixture):
    store, agent, transport, operations = fixture
    operations.dispatch_enabled = False
    first = operations.execute("fixture", request(agent))
    assert first.phase is Phase.ACCEPTED
    assert transport.creations == 0
    assert operations.recover("fixture") == [first]
    assert store.operation_journal.get("fixture", first.operation_id) == first


def test_no_selected_transport_never_falls_back(fixture):
    store, agent, transport, operations = fixture
    local = LostAcknowledgement()
    local.backend = "local"
    operations = Operations(store, [local])
    first = operations.execute("fixture", request(agent))
    assert first.phase is Phase.UNKNOWN
    assert operations.recover("fixture") == [first]
    assert not local.lookups
    assert not local.creations
    assert not transport.creations


def test_operation_identity_is_written_before_external_apply(fixture):
    store, agent, transport, operations = fixture
    original = transport.apply_operation
    observed = []

    def apply(operation, payload):
        observed.append(store.operation_journal.get("fixture", operation.operation_id))
        return original(operation, payload)

    transport.apply_operation = apply
    result = operations.execute("fixture", request(agent))
    assert observed == [result]
    assert observed[0].phase is Phase.UNKNOWN


def test_payload_is_not_persisted_and_errors_do_not_disclose_it(fixture):
    store, agent, transport, operations = fixture
    first = operations.execute("fixture", request(agent, payload={"prompt": "sensitive fixture content"}))
    stored = store.redis.hget(store.key("fixture", "runtime-operations"), first.operation_id)
    assert "sensitive fixture content" not in stored
    with pytest.raises(OperationConflict) as error:
        operations.execute("fixture", request(agent, payload={"prompt": "other sensitive content"}))
    assert "sensitive" not in str(error.value)


@pytest.mark.parametrize("exception", [TimeoutError, ConnectionError])
def test_transport_failure_in_observe_is_unknown_without_dispatch(fixture, exception):
    store, agent, transport, operations = fixture

    def observe(operation):
        raise exception("fixture transport failed")

    transport.observe_operation = observe
    first = operations.execute("fixture", request(agent))
    assert first.phase is Phase.UNKNOWN
    assert not transport.creations


def test_committed_result_is_monotonic_and_requires_current_generation(fixture):
    store, agent, transport, operations = fixture
    operations.execute("fixture", request(agent))
    applied = operations.execute("fixture", request(agent))
    assert operations.journal.settle("fixture", applied, Observation(Phase.UNKNOWN)) == applied
    assert operations.execute("fixture", request(agent)) == applied
    assert transport.creations == 1
    store.start_execution("fixture", replace(agent, execution_id="", generation=0), agent.execution_id)
    with pytest.raises(OperationConflict, match="stale"):
        operations.execute("fixture", request(agent))
    assert operations.journal.get("fixture", applied.operation_id) == applied


def test_concurrent_payload_conflict_is_atomic(fixture):
    from concurrent.futures import ThreadPoolExecutor

    store, agent, transport, operations = fixture

    def prepare(prompt):
        try:
            return store.operation_journal.prepare("fixture", request(agent, payload={"prompt": prompt}))
        except OperationConflict:
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(prepare, ("first", "second")))
    assert sum(result is not None for result in results) == 1
    assert len(store.operation_journal.records("fixture")) == 1
    assert not transport.creations


def test_spawn_identity_cannot_be_changed_by_a_new_retry_key(fixture):
    store, agent, transport, operations = fixture
    first = operations.execute("fixture", request(agent))
    retried = operations.execute("fixture", request(agent, key="different retry key"))
    assert retried.operation_id == first.operation_id
    assert retried.phase is Phase.APPLIED
    assert transport.creations == 1


@pytest.mark.parametrize("generation", [True, 0, -1, "1", 1.0])
def test_invalid_generation_is_rejected_before_journal_write(fixture, generation):
    store, agent, transport, operations = fixture
    with pytest.raises(OperationConflict, match="generation"):
        operations.execute("fixture", replace(request(agent), generation=generation))
    assert not store.operation_journal.records("fixture")
    assert not transport.creations


def test_snapshot_preserves_unresolved_journal_for_observation(fixture):
    import fakeredis

    store, agent, transport, operations = fixture
    first = operations.execute("fixture", request(agent))
    state = store.export("fixture")
    restored = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    restored.restore("fixture", state)
    recovered = Operations(restored, [transport], dispatch_enabled=False).recover("fixture")
    assert recovered == [replace(first, phase=Phase.APPLIED, result={"uid": "object-1"})]
    assert transport.creations == 1


def test_settle_cannot_overwrite_another_operation_identity(fixture):
    store, agent, transport, operations = fixture
    first = operations.execute("fixture", request(agent))
    before = store.redis.hgetall(store.key("fixture", "runtime-operations"))
    forged = replace(first, payload_digest="forged")
    with pytest.raises(OperationConflict, match="identity"):
        store.operation_journal.settle("fixture", forged, Observation(Phase.UNKNOWN))
    assert store.redis.hgetall(store.key("fixture", "runtime-operations")) == before


def test_payload_is_snapshotted_before_observation(fixture):
    store, agent, transport, operations = fixture
    payload = {"prompt": "initial"}
    submitted = request(agent, payload=payload)
    original = transport.observe_operation

    def observe(operation):
        payload["prompt"] = "changed"
        return original(operation)

    received = []

    def apply(operation, body):
        received.append(body)
        return Observation(Phase.UNKNOWN)

    transport.observe_operation = observe
    transport.apply_operation = apply
    operations.execute("fixture", submitted)
    assert received == [{"prompt": "initial"}]


@pytest.mark.parametrize("field,value", [("execution_id", ""), ("key", ""), ("action", "invalid")])
def test_invalid_request_cannot_create_a_journal_entry(fixture, field, value):
    store, agent, transport, operations = fixture
    with pytest.raises(OperationConflict, match="invalid"):
        operations.execute("fixture", replace(request(agent), **{field: value}))
    assert not operations.journal.records("fixture")
    assert not transport.lookups


def test_unadmitted_execution_cannot_create_a_journal_entry(fixture):
    store, agent, transport, operations = fixture
    with pytest.raises(OperationConflict, match="not admitted"):
        operations.execute("fixture", replace(request(agent), execution_id="missing"))
    assert operations.journal.get("fixture", "missing") is None
    assert not operations.journal.records("fixture")


def test_journal_settlement_refuses_a_missing_entry_and_changed_target(fixture):
    store, agent, transport, operations = fixture
    first = operations.journal.prepare("fixture", request(agent))
    with pytest.raises(OperationConflict, match="missing"):
        operations.journal.settle("fixture", replace(first, operation_id="missing"), Observation(Phase.UNKNOWN))
    with pytest.raises(OperationConflict, match="target"):
        operations.journal.settle("fixture", replace(first, target={}), Observation(Phase.UNKNOWN))
    assert operations.journal.get("fixture", first.operation_id) == first


def test_transaction_contention_is_bounded_and_never_dispatches(fixture, monkeypatch):
    from redis.exceptions import WatchError

    from scripts.swarm_v2.runtime.operations import WRITE_ATTEMPTS

    store, agent, transport, operations = fixture
    original = store.redis.pipeline
    attempts = []

    def pipeline():
        pipe = original()

        def watch(*keys):
            attempts.append(keys)
            raise WatchError

        pipe.watch = watch
        return pipe

    monkeypatch.setattr(store.redis, "pipeline", pipeline)
    with pytest.raises(OperationConflict, match="kept changing"):
        operations.execute("fixture", request(agent))
    assert len(attempts) == WRITE_ATTEMPTS
    assert all(
        keys == (store.key("fixture", "runtime-operations"), store.key("fixture", "executions")) for keys in attempts
    )
    assert not operations.journal.records("fixture")
    assert not transport.creations


def test_stale_attempt_cannot_commit_after_transport_returns(fixture):
    store, agent, transport, operations = fixture
    original = transport.apply_operation

    def replace_during_apply(operation, payload):
        store.start_execution("fixture", replace(agent, execution_id="", generation=0), agent.execution_id)
        transport.lose_ack = False
        return original(operation, payload)

    transport.apply_operation = replace_during_apply
    with pytest.raises(OperationConflict, match="stale"):
        operations.execute("fixture", request(agent))
    entries = operations.journal.records("fixture")
    assert len(entries) == 1
    assert entries[0].phase is Phase.UNKNOWN
    assert entries[0].result == {}


def test_changed_action_with_same_command_key_is_a_conflict(fixture):
    store, agent, transport, operations = fixture
    first = operations.execute("fixture", request(agent, "command", key="same"))
    with pytest.raises(OperationConflict, match="payload"):
        operations.execute("fixture", request(agent, "terminate", key="same"))
    assert operations.journal.get("fixture", first.operation_id) == first
    assert transport.creations == 1


@pytest.mark.parametrize("case", ["case_a", "case_b", "case_c"])
def test_package_acceptance_cases_pass_on_independent_fixtures(case):
    from tests import sv2_run03_cases

    result = getattr(sv2_run03_cases, case)()
    assert result["passed"]
    assert len(result["independent_fixtures"]) == 2
    assert all(run["passed"] for run in result["independent_fixtures"])


def test_stale_entry_cannot_stop_current_operation_recovery(fixture):
    store, agent, transport, operations = fixture
    stale = operations.execute("fixture", request(agent))
    current = store.start_execution("fixture", replace(agent, execution_id="", generation=0), agent.execution_id)
    transport.lose_ack = True
    pending = operations.execute("fixture", request(current))
    recovered = Operations(RedisStore(store.redis), [transport], dispatch_enabled=False).recover("fixture")
    by_id = {operation.operation_id: operation for operation in recovered}
    assert by_id[stale.operation_id].phase is Phase.REFUSED
    assert by_id[stale.operation_id].result == {}
    assert store.operation_journal.get("fixture", stale.operation_id) == stale
    assert by_id[pending.operation_id] == replace(pending, phase=Phase.APPLIED, result={"uid": "object-2"})
    assert store.operation_journal.get("fixture", pending.operation_id) == by_id[pending.operation_id]
    assert transport.creations == 2
    assert operations.runtime_ambiguous_operations("fixture") == 1


def test_legacy_store_construction_does_not_load_new_runtime_service():
    import subprocess
    import sys

    code = (
        "import sys; from scripts.swarm.store import RedisStore; RedisStore(object()); "
        "assert 'scripts.swarm_v2.runtime.operations' not in sys.modules"
    )
    subprocess.run([sys.executable, "-c", code], check=True)


def test_payload_digest_is_canonical_and_stable_across_key_order(fixture):
    store, agent, transport, operations = fixture
    first = operations.execute("fixture", request(agent, "command", {"b": {"d": 3, "c": 2}, "a": 1}, "canonical"))
    retried = operations.execute("fixture", request(agent, "command", {"a": 1, "b": {"c": 2, "d": 3}}, "canonical"))
    assert first.payload_digest == "b93739ed0e2241bce8df642a4d4273dd89eb7be70cda2a4e64a661e8f39f7023"
    assert retried.phase is Phase.APPLIED
    assert transport.creations == 1
    assert retried.action == "command"


@pytest.mark.parametrize("number", [float("nan"), float("inf"), float("-inf")])
def test_nonfinite_payload_is_rejected_without_persistence_or_dispatch(fixture, number):
    store, agent, transport, operations = fixture
    submitted = request(agent, payload={"number": number})
    with pytest.raises(ValueError):
        operations.execute("fixture", submitted)
    with pytest.raises(ValueError):
        store.operation_journal.prepare("fixture", submitted)
    assert not store.operation_journal.records("fixture")
    assert not transport.lookups


def test_runtime_journal_uses_one_redis_connection_per_transaction(fixture):
    store, agent, transport, operations = fixture
    store.redis.connection_pool.max_connections = 1
    first = operations.execute("fixture", request(agent))
    applied = operations.execute("fixture", request(agent))
    assert first.phase is Phase.UNKNOWN
    assert applied.phase is Phase.APPLIED
    assert applied.result == {"uid": "object-1"}
    assert transport.creations == 1


@pytest.mark.parametrize("phase", ["applied", "unsupported"])
def test_untyped_observation_state_cannot_bypass_ownership(fixture, phase):
    store, agent, transport, operations = fixture
    first = operations.execute("fixture", request(agent))
    transport.effects[first.operation_id] = replace(
        transport.effects[first.operation_id], phase=phase, execution_id="foreign"
    )
    rejected = operations.execute("fixture", request(agent))
    assert rejected.phase is Phase.REFUSED
    assert rejected.result == {}
    assert store.operation_journal.get("fixture", first.operation_id).phase is Phase.REFUSED
    assert transport.creations == 1


@pytest.mark.parametrize("generation", [True, 1.0])
def test_untyped_observation_generation_cannot_prove_applied_state(fixture, generation):
    store, agent, transport, operations = fixture
    first = operations.execute("fixture", request(agent))
    transport.effects[first.operation_id] = replace(transport.effects[first.operation_id], generation=generation)
    rejected = operations.execute("fixture", request(agent))
    assert rejected.phase is Phase.REFUSED
    assert rejected.result == {}
    assert transport.creations == 1
