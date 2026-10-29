import json
from dataclasses import replace
from pathlib import Path

import pytest

import scripts.swarm.execution as execution
from scripts.swarm import lease
from scripts.swarm.store import AgentRecord, RedisStore, SwarmConfig, SwarmError
from scripts.swarm_v2.controller import Controller
from scripts.swarm_v2.herdr.endpoints import Endpoint, Scope
from scripts.swarm_v2.herdr.reconcile import Report
from scripts.swarm_v2.herdr.upkeep import Attempt, Upkeep
from scripts.swarm_v2.kubernetes import watch
from scripts.swarm_v2.kubernetes.terminal import HostKeys
from scripts.swarm_v2.reconciliation import accounts as reconciliation
from scripts.swarm_v2.reconciliation.accounts import AccountReconciler
from scripts.swarm_v2.runtime import observe
from scripts.swarm_v2.runtime.operations import Observation, OperationRequest, Phase
from tests import sv2_hdr03_cases

pytestmark = pytest.mark.unit
HOST_KEY = sv2_hdr03_cases.HOST_KEY


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
    monkeypatch.setattr("time.time", lambda: clock[0] / 1000)
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
    clock[0] += lease.ttl_ms()
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
    clock[0] += lease.ttl_ms()
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
    assert first.ready is False
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


def test_a_renewal_refused_its_grant_closes_admission_until_a_granted_renewal(fixture):
    store, (first, _), transport, _, grant = fixture
    assert first.acquire()
    grant["allowed"] = False
    with pytest.raises(SwarmError, match="^a scoped controller grant is required$"):
        first.renew()
    assert first.ready is False
    grant["allowed"] = True
    with pytest.raises(SwarmError, match="^controller admission is disabled until reconciliation completes$"):
        first.admit(agent(store))
    assert first.renew() is True
    assert first.admit(agent(store)).generation == 1
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
    assert first.ready is False
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
                clock[0] += lease.ttl_ms()
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
            clock[0] += lease.ttl_ms()
            assert second.acquire()
        return result

    monkeypatch.setattr(transport, "observe_operation", interrupted)
    with pytest.raises(SwarmError):
        first.execute(request(attempt))
    assert transport.creations == 0


def test_direct_registry_admission_records_exact_intent(fixture):
    store, (first, _), _, _, _ = fixture
    assert first.acquire()
    candidate = agent(store)
    with lease.fencing(first.held.epoch):
        attempt = execution.ExecutionRegistry(store).start("fixture", candidate, "")
    assert first.intents() == [
        {
            "execution_id": attempt.execution_id,
            "generation": attempt.generation,
            "controller_epoch": first.held.epoch,
        }
    ]
    assert store.execution_registry.records("fixture") == [attempt]


def test_direct_registry_rejects_stale_epoch(fixture):
    store, (first, second), _, clock, _ = fixture
    assert first.acquire()
    candidate = agent(store)
    clock[0] += lease.ttl_ms()
    assert second.acquire()
    with lease.fencing(first.held.epoch), pytest.raises(SwarmError) as refused:
        execution.ExecutionRegistry(store).start("fixture", candidate, "")
    assert str(refused.value) == "the controller lease is stale"
    assert first.intents() == []
    assert store.execution_registry.records("fixture") == []


@pytest.mark.parametrize("conflict", ("leadership", "seat"))
def test_direct_registry_conflict_at_commit_leaves_only_current_authority(fixture, monkeypatch, conflict):
    store, (first, second), _, clock, _ = fixture
    assert first.acquire()
    candidate, successor = agent(store), agent(store)
    original = store.redis.pipeline
    once = [True]

    def pipeline():
        pipe = original()
        execute = pipe.execute

        def interrupted():
            if once[0]:
                once[0] = False
                if conflict == "leadership":
                    clock[0] += lease.ttl_ms()
                    assert second.acquire()
                else:
                    execution.ExecutionRegistry(store).start("fixture", successor, "")
            return execute()

        pipe.execute = interrupted
        return pipe

    monkeypatch.setattr(store.redis, "pipeline", pipeline)
    with lease.fencing(first.held.epoch), pytest.raises(SwarmError):
        execution.ExecutionRegistry(store).start("fixture", candidate, "")
    records = store.execution_registry.records("fixture")
    assert len(records) == (1 if conflict == "seat" else 0)
    assert all(record.name == successor.name for record in records)
    assert len(first.intents()) == len(records)


def test_unacquired_controller_refuses_authority_and_reports_no_leader_changes(fixture):
    _, (first, _), _, _, _ = fixture
    assert first.ready is False
    assert first.reconciled_epoch is None
    with pytest.raises(SwarmError) as refused:
        first.require()
    assert str(refused.value) == "the controller lease is absent"
    assert not first.renew()
    assert not first.release()
    assert first.controller_leader_changes_total() == 0


def test_reconciliation_holds_admission_disabled_inside_its_epoch(fixture, monkeypatch):
    _, (first, _), _, _, _ = fixture
    assert first.acquire()

    def recover(slug):
        assert slug == "fixture"
        assert first.ready is False
        assert first.reconciled_epoch is None
        assert lease.EPOCH.get() == first.held.epoch
        with pytest.raises(SwarmError) as refused:
            first.require()
        assert str(refused.value) == "controller admission is disabled until reconciliation completes"

    monkeypatch.setattr(first.operations, "recover", recover)
    assert first.acquire()
    assert first.ready is True


def test_scoped_grant_refusal_preserves_exact_error(fixture):
    _, (first, _), _, _, grant = fixture
    grant["allowed"] = False
    with pytest.raises(SwarmError) as refused:
        first.acquire()
    assert str(refused.value) == "a scoped controller grant is required"


def test_controller_replacement_forwards_prior_execution_identity(fixture):
    store, (first, _), _, _, _ = fixture
    assert first.acquire()
    prior = first.admit(agent(store))
    current = first.admit(agent(store), prior.execution_id)
    assert current.generation == prior.generation + 1
    assert store.execution_registry.occupants("fixture")[current.seat] == current
    assert len(first.intents()) == 2


def test_fenced_transport_preserves_command_payload(fixture, monkeypatch):
    store, (first, _), transport, _, _ = fixture
    assert first.acquire()
    attempt = first.admit(agent(store))
    command = request(attempt)
    original = transport.apply_operation

    def apply(operation, payload):
        assert payload == command.payload
        return original(operation, payload)

    monkeypatch.setattr(transport, "apply_operation", apply)
    assert first.execute(command).phase is Phase.APPLIED
    assert transport.creations == 1


@pytest.mark.parametrize("case", ("case_a", "case_b", "case_c"))
def test_package_acceptance_cases(case):
    from tests import sv2_ctl01_cases

    assert getattr(sv2_ctl01_cases, case)()["passed"]


class Pods:
    def __init__(self, pods):
        self.pods = {pod.uid: pod for pod in pods}
        self.deleted, self.events, self.expire, self.lists = [], [], False, 0
        self.before_read, self.before_delete, self.interrupt = lambda: None, lambda: None, None

    def list_pods(self, selector):
        self.lists += 1
        return [pod for pod in self.pods.values() if selector in pod.labels], str(self.lists)

    def watch_pods(self, selector, resource_version):
        if self.expire:
            self.expire = False
            raise watch.CursorExpired(resource_version)
        if self.interrupt:
            failure, self.interrupt, self.events = self.interrupt, None, []
            raise failure
        yield from self.events
        self.events = []

    def read_pod(self, name):
        self.before_read()
        return next((pod for pod in self.pods.values() if pod.name == name), None)

    def delete_pod(self, name, uid):
        self.before_delete()
        self.deleted.append((name, uid))
        self.events.append(("DELETED", self.pods.pop(uid), f"deleted-{uid}"))


def managed(name, execution_id, uid=None):
    return watch.Pod(name, uid or f"uid-{name}", watch.labels(watch.owner_for("fixture"), execution_id))


def observed(store, transport, grant, pods, cleanup=True):
    view = watch.PodView(pods)
    return Controller(store, "fixture", [transport], lambda: grant["allowed"], pods=view, orphan_cleanup=cleanup)


def launched(store, controller):
    assert controller.acquire()
    attempt = controller.admit(agent(store))
    assert controller.release()
    return attempt


def test_owner_label_is_derived_from_the_swarm_not_the_controller_process():
    assert watch.owner_for("fixture") == "agentihooks-swarm-fixture"


def test_restart_converges_to_existing_pods_and_deletes_only_managed_orphans(fixture):
    store, (first, _), transport, clock, grant = fixture
    attempt = launched(store, first)
    pods = Pods(
        [
            managed("eng-1", attempt.execution_id, uid="uid-live"),
            managed("eng-2", "exec-orphan"),
            watch.Pod("eng-2-copy", "uid-lookalike", {"app": "eng-2"}),
            watch.Pod("eng-3", "uid-other", watch.labels("agentihooks-swarm-other", "exec-x")),
        ]
    )
    clock[0] += lease.ttl_ms()
    restarted = observed(store, transport, grant, pods)
    assert restarted.acquire()
    assert pods.deleted == [("eng-2", "uid-eng-2")]
    assert sorted(pods.pods) == ["uid-live", "uid-lookalike", "uid-other"]
    assert transport.creations == 0
    assert restarted.controller_orphans_by_class() == {
        "managed_orphan": 1,
        "missing_pod": 0,
        "foreign": 1,
        "ambiguous": 0,
        "terminating": 0,
        "superseded": 0,
    }
    again = restarted.reconcile()
    assert again.matched == {attempt.execution_id: "uid-live"}
    assert again.delete == [] and pods.deleted == [("eng-2", "uid-eng-2")]


def test_orphan_cleanup_is_on_by_default(fixture):
    store, _, transport, _, grant = fixture
    pods = Pods([managed("eng-2", "exec-orphan")])
    controller = Controller(store, "fixture", [transport], lambda: grant["allowed"], pods=watch.PodView(pods))
    assert controller.acquire()
    assert pods.deleted == [("eng-2", "uid-eng-2")]


def test_journal_without_a_pod_is_reported_missing(fixture):
    store, (first, _), transport, clock, grant = fixture
    attempt = launched(store, first)
    restarted = observed(store, transport, grant, Pods([]))
    clock[0] += lease.ttl_ms()
    assert restarted.acquire()
    assert restarted.reconcile().missing_pods == [attempt.execution_id]
    assert restarted.controller_orphans_by_class()["missing_pod"] == 1


def test_ended_execution_without_a_pod_is_not_missing(fixture):
    store, (first, _), transport, clock, grant = fixture
    attempt = launched(store, first)
    store.put_agent("fixture", replace(attempt, state="ended"))
    restarted = observed(store, transport, grant, Pods([]))
    clock[0] += lease.ttl_ms()
    assert restarted.acquire()
    assert restarted.reconcile().missing_pods == []
    assert restarted.controller_orphans_by_class()["missing_pod"] == 0


def test_live_pod_of_an_ended_execution_is_kept(fixture):
    store, (first, _), transport, clock, grant = fixture
    attempt = launched(store, first)
    store.put_agent("fixture", replace(attempt, state="ended"))
    pods = Pods([managed("eng-1", attempt.execution_id, uid="uid-ended")])
    restarted = observed(store, transport, grant, pods)
    clock[0] += lease.ttl_ms()
    assert restarted.acquire()
    plan = restarted.reconcile()
    assert pods.deleted == [] and plan.missing_pods == []
    assert restarted.controller_orphans_by_class()["superseded"] == 1


def test_only_the_live_execution_without_a_pod_is_missing_beside_an_ended_one(fixture):
    store, (first, _), transport, clock, grant = fixture
    assert first.acquire()
    ended = first.admit(agent(store))
    live = first.admit(
        replace(
            agent(store),
            seat="eng-2@fixture",
            runtime_target={"pod_namespace": "workers", "pod_name": "attempt-2"},
        )
    )
    assert first.release()
    store.put_agent("fixture", replace(store.execution("fixture", ended.execution_id), state="ended"))
    assert {seat: row.state for seat, row in store.execution_occupants("fixture").items()} == {
        "eng-1@fixture": "ended",
        "eng-2@fixture": live.state,
    }
    restarted = observed(store, transport, grant, Pods([]))
    clock[0] += lease.ttl_ms()
    assert restarted.acquire()
    assert restarted.reconcile().missing_pods == [live.execution_id]
    assert restarted.controller_orphans_by_class()["missing_pod"] == 1


def test_orphan_replaced_before_delete_is_not_deleted(fixture):
    store, _, transport, _, grant = fixture
    pods = Pods([managed("eng-2", "exec-orphan")])

    def recreate():
        pods.pods = {"uid-new": managed("eng-2", "exec-orphan", uid="uid-new")}

    pods.before_read = recreate
    assert observed(store, transport, grant, pods).acquire()
    assert pods.deleted == []
    assert list(pods.pods) == ["uid-new"]


def test_orphan_relabelled_before_delete_is_not_deleted(fixture):
    store, _, transport, _, grant = fixture
    pods = Pods([managed("eng-2", "exec-orphan")])

    def relabel():
        pods.pods = {"uid-eng-2": watch.Pod("eng-2", "uid-eng-2", {"app": "eng-2"})}

    pods.before_read = relabel
    assert observed(store, transport, grant, pods).acquire()
    assert pods.deleted == []


def test_takeover_before_delete_refuses_the_delete(fixture):
    store, (_, second), transport, clock, grant = fixture
    pods = Pods([managed("eng-2", "exec-orphan")])
    controller = observed(store, transport, grant, pods)

    def takeover():
        clock[0] += lease.ttl_ms()
        assert second.acquire()

    pods.before_read = takeover
    with pytest.raises(SwarmError):
        controller.acquire()
    assert pods.deleted == []
    assert not controller.ready


def test_grant_revoked_between_read_and_delete_refuses_the_delete(fixture):
    store, _, transport, _, grant = fixture
    pods = Pods([managed("eng-2", "exec-orphan")])
    controller = observed(store, transport, grant, pods)
    pods.before_read = lambda: grant.update(allowed=False)
    with pytest.raises(SwarmError, match="^a scoped controller grant is required$"):
        controller.acquire()
    assert pods.deleted == []


def test_takeover_during_delete_keeps_the_new_leader_counts(fixture):
    store, (_, second), transport, clock, grant = fixture
    pods = Pods([])
    controller = observed(store, transport, grant, pods)
    assert controller.acquire()
    counts = store.redis.hgetall(store.key("fixture", "controller-orphans"))
    pods.pods = {"uid-eng-2": managed("eng-2", "exec-orphan")}

    def takeover():
        clock[0] += lease.ttl_ms()
        assert second.acquire()

    pods.before_delete, pods.expire = takeover, True
    with pytest.raises(SwarmError, match="^the controller lease is stale$"):
        controller.reconcile()
    assert pods.deleted == [("eng-2", "uid-eng-2")]
    assert store.redis.hgetall(store.key("fixture", "controller-orphans")) == counts


def test_disabled_cleanup_keeps_orphans_while_matching_continues(fixture):
    store, (first, _), transport, clock, grant = fixture
    attempt = launched(store, first)
    pods = Pods([managed("eng-1", attempt.execution_id, uid="uid-live"), managed("eng-2", "exec-orphan")])
    clock[0] += lease.ttl_ms()
    restarted = observed(store, transport, grant, pods, cleanup=False)
    assert restarted.acquire()
    assert pods.deleted == []
    assert restarted.reconcile().matched == {attempt.execution_id: "uid-live"}
    assert restarted.controller_orphans_by_class()["managed_orphan"] == 1


def test_expired_cursor_relists_and_cleans_the_new_orphan(fixture):
    store, _, transport, _, grant = fixture
    pods = Pods([])
    controller = observed(store, transport, grant, pods)
    assert controller.acquire()
    pods.pods = {"uid-eng-2": managed("eng-2", "exec-orphan")}
    pods.expire = True
    controller.reconcile()
    assert pods.lists == 2
    assert pods.deleted == [("eng-2", "uid-eng-2")]


def test_reconcile_requires_the_lease_and_reports_nothing_before(fixture):
    store, _, transport, _, grant = fixture
    controller = observed(store, transport, grant, Pods([managed("eng-2", "exec-orphan")]))
    assert controller.controller_orphans_by_class() == dict.fromkeys(watch.CLASSES, 0)
    with pytest.raises(SwarmError, match="^the controller lease is absent$"):
        controller.reconcile()


def test_controller_without_a_pod_view_reconciles_nothing(fixture):
    store, (first, _), _, _, _ = fixture
    assert first.acquire()
    assert first.reconcile() is None
    assert first.controller_orphans_by_class() == dict.fromkeys(watch.CLASSES, 0)
    assert not store.redis.exists(store.key("fixture", "controller-orphans"))


def test_live_pod_of_a_superseded_generation_is_kept(fixture):
    store, (first, _), transport, clock, grant = fixture
    assert first.acquire()
    prior = first.admit(agent(store))
    current = first.admit(agent(store), prior.execution_id)
    assert first.release()
    pods = Pods([managed("eng-1", prior.execution_id, uid="uid-prior")])
    clock[0] += lease.ttl_ms()
    restarted = observed(store, transport, grant, pods)
    assert restarted.acquire()
    plan = restarted.reconcile()
    assert pods.deleted == []
    assert plan.missing_pods == [current.execution_id]
    assert restarted.controller_orphans_by_class()["superseded"] == 1


@pytest.mark.parametrize("failure", (ConnectionError("watch reset"), TimeoutError("watch stalled")))
def test_interrupted_watch_relists_and_converges(fixture, failure):
    store, _, transport, _, grant = fixture
    pods = Pods([])
    controller = observed(store, transport, grant, pods)
    assert controller.acquire()
    pods.pods = {"uid-eng-2": managed("eng-2", "exec-orphan")}
    pods.events, pods.interrupt = [("ADDED", managed("eng-2", "exec-orphan"), "5")], failure
    controller.reconcile()
    assert pods.lists == 2
    assert pods.deleted == [("eng-2", "uid-eng-2")]


def test_revoked_grant_refuses_reconcile_without_deleting(fixture):
    store, _, transport, _, grant = fixture
    pods = Pods([])
    controller = observed(store, transport, grant, pods)
    assert controller.acquire()
    pods.pods = {"uid-eng-2": managed("eng-2", "exec-orphan")}
    grant["allowed"] = False
    with pytest.raises(SwarmError, match="^a scoped controller grant is required$"):
        controller.reconcile()
    assert pods.deleted == []


@pytest.mark.parametrize("case", ("a", "b", "c"))
def test_reconcile_package_cases_pass_from_independent_state(case):
    from tests import sv2_ctl04_cases

    first, second = sv2_ctl04_cases.run_case(case), sv2_ctl04_cases.run_case(case)
    assert first == second
    assert first["state"] == "passed"


RECONCILE = json.loads((Path(__file__).parent / "fixtures/swarm_v2/account-reconciliation.json").read_text())
TTL, TERMINAL = RECONCILE["ttl_ms"], RECONCILE["terminal_seat"]
LOST_TERMINAL, ORPHANED, SUCCESSOR = (
    f"fixture/{RECONCILE[seat]}" for seat in ("terminal_seat", "orphan_seat", "handoff_seat")
)
RETIRING = f"{SUCCESSOR}#1"


@pytest.fixture
def accounts(monkeypatch):
    from tests.test_swarm_v2_account_reconciliation import World

    monkeypatch.setenv("AGENTIHOOKS_CONTROLLER_TICK_SECONDS", "20")
    world = World(monkeypatch)
    world.scene()
    return world


def test_a_restarted_controller_reconciles_accounts_at_start_with_no_manual_step(accounts):
    accounts.clock[0] += lease.ttl_ms()
    assert sorted(accounts.rows()) == sorted([LOST_TERMINAL, RETIRING, ORPHANED, SUCCESSOR])
    restarted = Controller(accounts.store, "fixture", [], lambda: True)

    assert restarted.acquire()

    assert sorted(accounts.rows()) == sorted([LOST_TERMINAL, RETIRING])
    assert restarted.accounts.account_occupancy_discrepancies() == {"handoff_unconfirmed": 1}


def test_each_renew_tick_reconciles_accounts(accounts):
    controller = accounts.authority.controller
    accounts.clock[0] += TTL

    assert sorted(accounts.rows()) == sorted([LOST_TERMINAL, RETIRING, ORPHANED, SUCCESSOR])
    assert controller.renew() is True
    assert sorted(accounts.rows()) == sorted([LOST_TERMINAL, RETIRING])


def test_a_renew_tick_of_an_unreconciled_epoch_releases_nothing_and_stays_closed(accounts):
    controller = accounts.authority.controller
    controller.reconciled_epoch = None
    accounts.clock[0] += TTL
    before = accounts.rows()

    assert controller.renew() is False
    assert controller.ready is False
    assert accounts.rows() == before


def test_a_renew_tick_whose_account_reconcile_fails_leaves_admission_closed(accounts, monkeypatch):
    controller = accounts.authority.controller

    def unavailable():
        raise SwarmError("dependency_unavailable")

    monkeypatch.setattr(controller.accounts, "reconcile", unavailable)

    with pytest.raises(SwarmError, match="^dependency_unavailable$"):
        controller.renew()
    assert controller.ready is False


def test_a_grant_revoked_while_the_lease_renews_releases_nothing_and_closes_admission(accounts, monkeypatch):
    controller = accounts.authority.controller
    renew = lease.renew
    accounts.clock[0] += TTL
    before = accounts.rows()

    def revoked(store, slug, held):
        monkeypatch.setattr(controller, "authorize", lambda: False)
        return renew(store, slug, held)

    monkeypatch.setattr(lease, "renew", revoked)
    with pytest.raises(SwarmError, match="^a scoped controller grant is required$"):
        controller.renew()
    assert controller.ready is False
    assert accounts.rows() == before


def test_an_interrupted_renewal_leaves_admission_closed(fixture, monkeypatch):
    store, (first, _), _, _, _ = fixture
    assert first.acquire()

    def interrupted(*args):
        raise KeyboardInterrupt

    monkeypatch.setattr(lease, "renew", interrupted)
    with pytest.raises(KeyboardInterrupt):
        first.renew()
    assert first.ready is False


def test_a_refused_renew_tick_releases_nothing(accounts):
    controller = accounts.authority.controller
    accounts.clock[0] += lease.ttl_ms()
    before = accounts.rows()

    assert controller.renew() is False
    assert accounts.rows() == before


def exit_signal(agent, source, value, at):
    return observe.Signal(source, observe.Reading.OK, at, agent.execution_id, agent.generation, value)


@pytest.mark.parametrize(
    ("source", "value"),
    (
        (observe.Source.SUPERVISOR, observe.EXITED),
        (observe.Source.KUBERNETES, "Failed"),
        (observe.Source.KUBERNETES, "Succeeded"),
    ),
)
def test_an_exit_observation_reaches_the_account_exit_call(accounts, source, value):
    controller, lost = accounts.authority.controller, accounts.agents[TERMINAL]
    observer = observe.Observer(accounts.store, "local", observe.Thresholds())
    now = accounts.clock[0] / 1000

    found = controller.observe(observer, lost, [exit_signal(lost, source, value, now)], now)

    assert (found.kind, found.action, found.holder) == ("exited", "release", LOST_TERMINAL)
    assert (found.execution_id, found.generation, found.evidence) == (lost.execution_id, lost.generation, source.value)
    assert sorted(accounts.rows()) == sorted([RETIRING, ORPHANED, SUCCESSOR])
    assert observer.get("fixture", lost.execution_id).sources[source.value]["value"] == value


def test_an_exit_observation_records_its_heartbeat_and_outcome_transitions(accounts):
    controller, lost = accounts.authority.controller, accounts.agents[TERMINAL]
    observer = observe.Observer(accounts.store, "local", observe.Thresholds())
    now = accounts.clock[0] / 1000

    found = controller.observe(observer, lost, [exit_signal(lost, observe.Source.SUPERVISOR, observe.EXITED, now)], now)

    rows = [
        (
            e["transition"],
            e["result"],
            e.get("task", ""),
            e.get("seat", ""),
            e["expected_generation"],
            e["observed_generation"],
        )
        for e in controller.telemetry.events(execution_id=lost.execution_id)
        if e["transition"] in ("heartbeat", "outcome")
    ]
    identity = (lost.task, lost.seat, lost.generation, lost.generation)
    assert rows == [("heartbeat", "accepted", *identity), ("outcome", "accepted", *identity)]
    outcome = controller.telemetry.events(transition="outcome")[0]
    assert (outcome["reason"], outcome["controller_epoch"]) == (found.evidence, controller.held.epoch)


@pytest.mark.parametrize("stored", (True, False))
def test_a_repeated_exit_observation_reaches_the_exit_call_once(accounts, stored):
    controller, lost = accounts.authority.controller, accounts.agents[TERMINAL]
    observer = observe.Observer(accounts.store, "local", observe.Thresholds())
    if not stored:
        accounts.store.redis.hdel(accounts.store.key("fixture", "observations"), lost.execution_id)
    now = accounts.clock[0] / 1000
    signal = exit_signal(lost, observe.Source.SUPERVISOR, observe.EXITED, now)

    first = controller.observe(observer, lost, [signal], now)
    again = controller.observe(observer, lost, [signal], now + 1)

    assert (first.kind, first.action) == ("exited", "release")
    assert again is None
    assert controller.accounts.stale_exit_events() == 0
    assert sorted(accounts.rows()) == sorted([RETIRING, ORPHANED, SUCCESSOR])


def test_an_exit_whose_release_was_held_back_is_released_on_a_later_observation(accounts):
    controller, lost = accounts.authority.controller, accounts.agents[TERMINAL]
    observer = observe.Observer(accounts.store, "local", observe.Thresholds())
    now = accounts.clock[0] / 1000
    signal = exit_signal(lost, observe.Source.SUPERVISOR, observe.EXITED, now)
    controller.accounts = AccountReconciler(accounts.store, "fixture", environ={reconciliation.MODE: "observe"})

    held = controller.observe(observer, lost, [signal], now)
    assert (held.kind, held.action) == ("exited", "observe_only")
    assert LOST_TERMINAL in accounts.rows()

    controller.accounts = AccountReconciler(accounts.store, "fixture", environ={})
    retried = controller.observe(observer, lost, [signal], now + 1)

    assert (retried.kind, retried.action, retried.holder) == ("exited", "release", LOST_TERMINAL)
    assert LOST_TERMINAL not in accounts.rows()
    assert controller.accounts.stale_exit_events() == 0


def test_a_second_source_reporting_the_same_exit_is_not_a_stale_exit(accounts):
    controller, lost = accounts.authority.controller, accounts.agents[TERMINAL]
    observer = observe.Observer(accounts.store, "local", observe.Thresholds())
    now = accounts.clock[0] / 1000

    first = controller.observe(observer, lost, [exit_signal(lost, observe.Source.SUPERVISOR, observe.EXITED, now)], now)
    later = controller.observe(observer, lost, [exit_signal(lost, observe.Source.KUBERNETES, "Failed", now)], now)

    assert (first.kind, first.evidence) == ("exited", "supervisor")
    assert later is None
    assert controller.accounts.stale_exit_events() == 0


def test_an_observation_without_an_exit_frees_no_slot(accounts):
    controller, lost = accounts.authority.controller, accounts.agents[TERMINAL]
    observer = observe.Observer(accounts.store, "local", observe.Thresholds())
    now = accounts.clock[0] / 1000
    before = accounts.rows()

    found = controller.observe(observer, lost, [exit_signal(lost, observe.Source.KUBERNETES, "Running", now)], now)

    assert found is None
    assert accounts.rows() == before
    assert observer.get("fixture", lost.execution_id).sources["kubernetes"]["value"] == "Running"
    assert controller.accounts.stale_exit_events() == 0


def test_an_exit_observation_needs_the_controller_lease(accounts):
    controller, lost = accounts.authority.controller, accounts.agents[TERMINAL]
    observer = observe.Observer(accounts.store, "local", observe.Thresholds())
    accounts.clock[0] += lease.ttl_ms()
    now = accounts.clock[0] / 1000
    before, seen = accounts.rows(), observer.get("fixture", lost.execution_id)

    with pytest.raises(SwarmError, match="^the controller lease is stale$"):
        controller.observe(observer, lost, [exit_signal(lost, observe.Source.SUPERVISOR, observe.EXITED, now)], now)

    assert accounts.rows() == before
    assert observer.get("fixture", lost.execution_id) == seen


class Keeper:
    def __init__(self) -> None:
        self.calls: list[tuple[str, list]] = []

    def start(self, attempts):
        self.calls.append(("start", list(attempts)))
        return Report()

    def change(self, attempts):
        self.calls.append(("change", list(attempts)))

    def herdr_registration_drift(self) -> int:
        return 3


def keeping(store, grant, terminals):
    controller = Controller(store, "fixture", [Transport()], lambda: grant["allowed"])
    controller.terminals = terminals
    return controller


def attachable(store, attempt) -> Attempt:
    HostKeys(store).record(Scope("grant/x", "fixture", attempt.execution_id, attempt.generation), HOST_KEY)
    return Attempt(Scope("operator/operator", "fixture", attempt.execution_id, attempt.generation), HOST_KEY)


def test_a_starting_controller_reconciles_the_attachable_attempts(fixture):
    store, (first, _second), _transport, _clock, grant = fixture
    assert first.acquire()
    wanted = attachable(store, first.admit(agent(store)))
    assert first.release()
    keeper = Keeper()

    assert keeping(store, grant, keeper).acquire()

    assert keeper.calls == [("start", [wanted])]


class Reporting(Keeper):
    def start(self, attempts):
        super().start(attempts)
        return Report(
            registered={"exec-b": "p2", "exec-a": "p1"},
            refused={"exec-d": "machine_refused", "exec-c": "catalog_unreachable"},
        )


def test_a_starting_controller_logs_the_machines_it_registered(fixture, capsys):
    store, _controllers, _transport, _clock, grant = fixture
    capsys.readouterr()

    assert keeping(store, grant, Reporting()).acquire()

    assert capsys.readouterr().out == (
        "controller: kept saved herdr machines for fixture on start: registered 2, "
        "refused {'exec-c': 'catalog_unreachable', 'exec-d': 'machine_refused'}\n"
    )


def test_a_controller_without_a_herdr_client_logs_no_machines(fixture, capsys):
    store, (first, _second), _transport, _clock, grant = fixture
    capsys.readouterr()

    assert first.acquire()

    assert capsys.readouterr().out == ""


def test_every_renewal_passes_the_attachable_attempts_for_change(fixture):
    store, _controllers, _transport, _clock, grant = fixture
    keeper = Keeper()
    controller = keeping(store, grant, keeper)
    assert controller.acquire()
    wanted = attachable(store, controller.admit(agent(store)))

    assert controller.renew()

    assert keeper.calls == [("start", []), ("change", [wanted])]


class Routes:
    def __init__(self) -> None:
        self.published = []

    def publish(self, attempts):
        self.published.append(list(attempts))


def test_the_controller_lists_its_live_worker_terminals_for_the_jump(fixture):
    store, _controllers, _transport, _clock, grant = fixture
    routes = Routes()
    controller = keeping(store, grant, None)
    controller.jump = routes
    assert controller.acquire()
    wanted = attachable(store, controller.admit(agent(store)))

    assert controller.renew()

    assert routes.published == [[], [wanted]]


def jumping(store, grant):
    controller, routes = keeping(store, grant, None), Routes()
    controller.jump = routes
    return controller, routes


def test_a_worker_that_ended_leaves_the_jump_on_the_next_renewal(fixture):
    store, _controllers, _transport, _clock, grant = fixture
    controller, routes = jumping(store, grant)
    assert controller.acquire()
    ended = controller.admit(agent(store))
    attachable(store, ended)
    assert controller.renew()
    HostKeys(store).forget("fixture", ended.execution_id)

    assert controller.renew()

    assert [len(published) for published in routes.published] == [0, 1, 0]


def test_a_controller_that_lost_its_lease_withdraws_every_jump_route(fixture):
    store, (first, _second), _transport, clock, grant = fixture
    controller, routes = jumping(store, grant)
    assert controller.acquire()
    attachable(store, controller.admit(agent(store)))
    assert controller.renew()
    clock[0] += lease.ttl_ms()
    assert first.acquire()

    assert not controller.renew()

    assert [len(published) for published in routes.published] == [0, 1, 0]


def test_a_controller_refused_the_lease_lists_no_jump_route(fixture):
    store, (first, _second), _transport, _clock, grant = fixture
    assert first.acquire()
    controller, routes = jumping(store, grant)

    assert not controller.acquire()

    assert routes.published == [[]]


def test_a_controller_whose_epoch_moved_withdraws_every_jump_route(fixture):
    store, _controllers, _transport, _clock, grant = fixture
    controller, routes = jumping(store, grant)
    assert controller.acquire()
    attachable(store, controller.admit(agent(store)))
    assert controller.renew()
    controller.reconciled_epoch = -1

    assert not controller.renew()

    assert [len(published) for published in routes.published] == [0, 1, 0]


def test_a_controller_whose_grant_was_revoked_withdraws_every_jump_route(fixture):
    store, _controllers, _transport, _clock, grant = fixture
    controller, routes = jumping(store, grant)
    assert controller.acquire()
    attachable(store, controller.admit(agent(store)))
    assert controller.renew()
    grant["allowed"] = False

    with pytest.raises(SwarmError):
        controller.renew()

    assert [len(published) for published in routes.published] == [0, 1, 0]


def test_a_controller_that_lost_its_lease_reconciles_nothing(fixture):
    store, (first, _second), _transport, clock, grant = fixture
    keeper = Keeper()
    controller = keeping(store, grant, keeper)
    assert controller.acquire()
    clock[0] += lease.ttl_ms()
    assert first.acquire()

    assert not controller.renew()

    assert keeper.calls == [("start", [])]


def test_the_controller_reports_the_registration_drift(fixture):
    store, (first, _second), _transport, _clock, grant = fixture

    assert keeping(store, grant, Keeper()).herdr_registration_drift() == 3
    assert first.herdr_registration_drift() is None
    grant["allowed"] = False
    with pytest.raises(SwarmError) as refused:
        keeping(store, grant, Keeper()).herdr_registration_drift()
    assert str(refused.value) == "a scoped controller grant is required"


class ReadyHerdr(sv2_hdr03_cases.Herdr):
    def __call__(self, command):
        if command[:3] == ["herdr", "machine", "add"]:
            self.ready.add(command[-1])
        return super().__call__(command)


def herdr_client(tmp_path):
    herdr = ReadyHerdr([sv2_hdr03_cases.fixture()["manual"]], set())
    return sv2_hdr03_cases.client_terminal(tmp_path / "client", herdr, "workers"), herdr


def test_start_and_an_attempt_change_each_save_the_client_machines(fixture, tmp_path):
    store, (first, _second), _transport, _clock, grant = fixture
    terminal, herdr = herdr_client(tmp_path)
    assert first.acquire()
    one = first.admit(agent(store))
    attachable(store, one)
    assert first.release()
    controller = keeping(store, grant, Upkeep(terminal))

    assert controller.acquire()
    assert sorted(terminal.registrations.store.load()) == [one.execution_id]
    assert controller.herdr_registration_drift() == 1
    target = {"pod_namespace": "workers", "pod_name": "attempt-two"}
    two = controller.admit(replace(agent(store), seat="eng-2@fixture", runtime_target=target))
    attachable(store, two)
    assert controller.renew()

    assert sorted(terminal.registrations.store.load()) == sorted([one.execution_id, two.execution_id])
    assert [machine["label"] for machine in herdr.machines] == [
        "devbox",
        Endpoint(Scope("operator/operator", "fixture", one.execution_id, one.generation), "workers").name,
        Endpoint(Scope("operator/operator", "fixture", two.execution_id, two.generation), "workers").name,
    ]
    assert controller.herdr_registration_drift() == 1
