import json
from collections.abc import Callable, Iterable
from uuid import uuid4

from scripts.swarm import lease
from scripts.swarm.store import AgentRecord, RedisStore, SwarmError
from scripts.swarm_v2.herdr.upkeep import Attempt, Upkeep
from scripts.swarm_v2.kubernetes.terminal import HostKeys
from scripts.swarm_v2.kubernetes.watch import BACKEND, CLASSES, Plan, Pod, PodView, Reconciler, owner_for
from scripts.swarm_v2.operator_auth import OPERATOR
from scripts.swarm_v2.reconciliation.accounts import AccountReconciler, Finding, exit_source
from scripts.swarm_v2.runtime.observe import Observer, Signal
from scripts.swarm_v2.runtime.operations import Observation, Operation, OperationRequest, Operations, OperationTransport
from scripts.swarm_v2.telemetry import Telemetry


class FencedTransport:
    def __init__(self, transport: OperationTransport, require: Callable[[], None]) -> None:
        self.transport, self.require = transport, require
        self.backend = transport.backend

    def observe_operation(self, operation: Operation) -> Observation:
        return self.transport.observe_operation(operation)

    def apply_operation(self, operation: Operation, payload: dict) -> Observation:
        self.require()
        return self.transport.apply_operation(operation, payload)

    def spec_digest(self, payload: dict) -> str:
        render = getattr(self.transport, "spec_digest", None)
        return render(payload) if render else ""


class Controller:
    """The authorization callback must validate a scoped controller credential, never a display label."""

    def __init__(
        self,
        store: RedisStore,
        slug: str,
        transports: Iterable[OperationTransport],
        authorize: Callable[[], bool],
        admission_enabled: bool = True,
        pods: PodView | None = None,
        orphan_cleanup: bool = True,
    ) -> None:
        self.store, self.slug, self.authorize = store, slug, authorize
        self.telemetry = Telemetry(store, slug)
        self.terminals: Upkeep | None = None
        self.jump = None
        self.host_keys = HostKeys(store)
        self.pods, self.reconciler = pods, Reconciler(owner_for(slug), orphan_cleanup)
        self.accounts = AccountReconciler(store, slug)
        self.owner = f"controller-{uuid4().hex}"
        self.held = None
        self.ready = False
        self.reconciled_epoch = None
        self.admission_enabled = admission_enabled
        self.operations = Operations(store, [FencedTransport(transport, self.require) for transport in transports])

    def acquire(self) -> bool:
        self.ready = False
        self.reconciled_epoch = None
        self._authorize()
        self.held = lease.acquire(self.store, self.slug, self.owner)
        if self.held is None:
            self._route([])
            return False
        with lease.fencing(self.held.epoch):
            self.operations.recover(self.slug)
        self.reconcile()
        self._authority()
        self.accounts.reconcile()
        self._authority()
        if self.terminals is not None:
            report = self.terminals.start(self._attempts())
            print(
                f"controller: kept saved herdr machines for {self.slug} on start: registered "
                f"{len(report.registered)}, refused {dict(sorted(report.refused.items()))}"
            )
        self._route(self._attempts())
        self.reconciled_epoch = self.held.epoch
        self.ready = True
        return True

    def renew(self) -> bool:
        try:
            return self._renew()
        except BaseException:
            self.ready = False
            self._route([])
            raise

    def _renew(self) -> bool:
        self._authorize()
        if self.held is None:
            return False
        try:
            self.held = lease.renew(self.store, self.slug, self.held)
        except SwarmError:
            self.held = None
            self.ready = False
            self._route([])
            return False
        if self.reconciled_epoch != self.held.epoch:
            self.ready = False
            self._route([])
            return False
        self._authority()
        self.accounts.reconcile()
        if self.terminals is not None:
            self.terminals.change(self._attempts())
        self._route(self._attempts())
        self.ready = True
        return True

    def _route(self, attempts: list[Attempt]) -> None:
        if self.jump is not None:
            self.jump.publish(attempts)

    def _authorize(self) -> None:
        if self.authorize() is not True:
            raise SwarmError("a scoped controller grant is required")

    def _authority(self) -> None:
        self._authorize()
        if self.held is None:
            raise SwarmError("the controller lease is absent")
        lease.require(self.store, self.slug, self.held)

    def require(self) -> None:
        self._authority()
        if not self.ready or not self.admission_enabled:
            raise SwarmError("controller admission is disabled until reconciliation completes")

    def reconcile(self) -> Plan | None:
        self._authority()
        if self.pods is None:
            return None
        occupants = self.store.execution_occupants(self.slug).values()
        journals = {agent.execution_id for agent in occupants if agent.runtime_backend == BACKEND}
        records = {agent.execution_id for agent in self.store.execution_registry.records(self.slug)}
        plan = self.reconciler.plan(journals, self.pods.sync().pods(), records - journals)
        for pod in plan.delete:
            self._delete_orphan(pod)
        self._authority()
        self.store.redis.hset(self.store.key(self.slug, "controller-orphans"), mapping=plan.counts())
        return plan

    def _delete_orphan(self, pod: Pod) -> None:
        if self.pods.source.read_pod(pod.name) != pod:
            return
        self._authority()
        self.pods.source.delete_pod(pod.name, pod.uid)

    def observe(self, observer: Observer, agent: AgentRecord, signals: Iterable[Signal], now: float) -> Finding | None:
        identity = {"task": agent.task, "seat": agent.seat, "execution_id": agent.execution_id}
        with self.telemetry.timed("heartbeat", **identity, expected_generation=agent.generation) as seen:
            self._authority()
            seen["controller_epoch"] = self.held.epoch
            before = observer.get(self.slug, agent.execution_id)
            found = observer.observe(self.slug, agent, signals, now)
            changed = before is None or before.state != found.state
            seen.update(observed_generation=found.generation, reason=found.state.value, audit=changed)
        source = exit_source(found)
        if source is None or not self.accounts.holds(found.execution_id, found.generation):
            return None
        with self.telemetry.timed("outcome", **identity, expected_generation=agent.generation) as seen:
            self._authority()
            finding = self.accounts.exited(found.execution_id, found.generation, source)
            seen.update(controller_epoch=self.held.epoch, observed_generation=found.generation, reason=source)
            return finding

    def admit(self, agent: AgentRecord, previous_execution_id: str = "") -> AgentRecord:
        identity = {"task": agent.task, "seat": agent.seat, "previous_execution_id": previous_execution_id}
        with self.telemetry.timed("admission", **identity) as seen:
            occupant = self.store.execution_occupants(self.slug).get(agent.seat)
            seen["expected_generation"] = occupant.generation + 1 if occupant else 1
            self.require()
            seen["controller_epoch"] = self.held.epoch
            with lease.fencing(self.held.epoch):
                admitted = self.store.start_execution(self.slug, agent, previous_execution_id)
            seen.update(execution_id=admitted.execution_id, observed_generation=admitted.generation)
            return admitted

    def execute(self, request: OperationRequest) -> Operation:
        identity = {"execution_id": request.execution_id, "action": request.action}
        with self.telemetry.timed("command", **identity, expected_generation=request.generation) as seen:
            self.require()
            seen["controller_epoch"] = self.held.epoch
            with lease.fencing(self.held.epoch):
                operation = self.operations.execute(self.slug, request)
            seen.update(
                operation_id=operation.operation_id, observed_generation=operation.generation, reason=operation.phase
            )
            return operation

    def release(self) -> bool:
        self.ready = False
        self._authorize()
        return self.held is not None and lease.release(self.store, self.slug, self.held)

    def intents(self) -> list[dict]:
        self._authorize()
        return [json.loads(raw) for raw in self.store.redis.hvals(self.store.key(self.slug, "controller-intents"))]

    def controller_leader_changes_total(self) -> int:
        self._authorize()
        return int(self.store.redis.get(self.store.key(self.slug, "controller-leader-changes")) or 0)

    def controller_orphans_by_class(self) -> dict[str, int]:
        self._authorize()
        counts = self.store.redis.hgetall(self.store.key(self.slug, "controller-orphans"))
        return {name: int(counts.get(name, 0)) for name in CLASSES}

    def _attempts(self) -> list[Attempt]:
        return self.host_keys.attempts(self.slug, self.store.execution_occupants(self.slug).values(), OPERATOR)

    def herdr_registration_drift(self) -> int | None:
        self._authorize()
        return None if self.terminals is None else self.terminals.herdr_registration_drift()
