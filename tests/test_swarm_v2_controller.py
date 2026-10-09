from dataclasses import replace

import pytest

import scripts.swarm.execution as execution
from scripts.swarm import lease
from scripts.swarm.store import AgentRecord, RedisStore, SwarmConfig, SwarmError
from scripts.swarm_v2.controller import Controller
from scripts.swarm_v2.kubernetes import watch
from scripts.swarm_v2.runtime.operations import Observation, OperationRequest, Phase

pytestmark = [pytest.mark.unit, pytest.mark.xdist_group("fakeredis")]


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
    clock[0] += lease.TTL_MS
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
                    clock[0] += lease.TTL_MS
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
    clock[0] += lease.TTL_MS
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


def test_journal_without_a_pod_is_reported_missing(fixture):
    store, (first, _), transport, clock, grant = fixture
    attempt = launched(store, first)
    restarted = observed(store, transport, grant, Pods([]))
    clock[0] += lease.TTL_MS
    assert restarted.acquire()
    assert restarted.reconcile().missing_pods == [attempt.execution_id]
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
        clock[0] += lease.TTL_MS
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
        clock[0] += lease.TTL_MS
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
    clock[0] += lease.TTL_MS
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
    clock[0] += lease.TTL_MS
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
