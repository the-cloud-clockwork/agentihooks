import json
from collections.abc import Callable, Iterable
from uuid import uuid4

from scripts.swarm import lease
from scripts.swarm.store import AgentRecord, RedisStore, SwarmError
from scripts.swarm_v2.kubernetes.watch import BACKEND, CLASSES, Plan, Pod, PodView, Reconciler, owner_for
from scripts.swarm_v2.reconciliation.accounts import AccountReconciler, Finding, exit_source
from scripts.swarm_v2.runtime.observe import Observer, Signal
from scripts.swarm_v2.runtime.operations import Observation, Operation, OperationRequest, Operations, OperationTransport


class FencedTransport:
    def __init__(self, transport: OperationTransport, require: Callable[[], None]) -> None:
        self.transport, self.require = transport, require
        self.backend = transport.backend

    def observe_operation(self, operation: Operation) -> Observation:
        return self.transport.observe_operation(operation)

    def apply_operation(self, operation: Operation, payload: dict) -> Observation:
        self.require()
        return self.transport.apply_operation(operation, payload)


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
            return False
        with lease.fencing(self.held.epoch):
            self.operations.recover(self.slug)
        self.reconcile()
        self.accounts.reconcile()
        self._authority()
        self.reconciled_epoch = self.held.epoch
        self.ready = True
        return True

    def renew(self) -> bool:
        self.ready = False
        self._authorize()
        if self.held is None:
            return False
        try:
            self.held = lease.renew(self.store, self.slug, self.held)
        except SwarmError:
            self.held = None
            return False
        if self.reconciled_epoch == self.held.epoch:
            self.accounts.reconcile()
            self.ready = True
        return self.ready

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
        self._authority()
        prior = observer.get(self.slug, agent.execution_id)
        seen = observer.observe(self.slug, agent, signals, now)
        source = exit_source(seen)
        if source is None or (
            prior is not None and prior.generation == seen.generation and exit_source(prior) is not None
        ):
            return None
        self._authority()
        return self.accounts.exited(seen.execution_id, seen.generation, source)

    def admit(self, agent: AgentRecord, previous_execution_id: str = "") -> AgentRecord:
        self.require()
        with lease.fencing(self.held.epoch):
            return self.store.start_execution(self.slug, agent, previous_execution_id)

    def execute(self, request: OperationRequest) -> Operation:
        self.require()
        with lease.fencing(self.held.epoch):
            return self.operations.execute(self.slug, request)

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
