"""Distributed spawn: the controller admits the execution, issues its launch grant and reserves the account slot before
the runtime launches anything; the worker's registration turns the reservation into occupancy and its exit releases it.
The capacity and registry authorize with LaunchAuthority.verify, so only the worker's registration confirms a grant."""

from dataclasses import dataclass, replace

from scripts.swarm.store import AgentRecord, SwarmError
from scripts.swarm_v2.accounts import AccountCapacity, Slot
from scripts.swarm_v2.auth_context import LaunchAuthority
from scripts.swarm_v2.controller import Controller
from scripts.swarm_v2.registry import FleetRegistry, Session
from scripts.swarm_v2.runtime.base import Outcome, RuntimeRouter, SpawnRequest, Status

NOT_LAUNCHED = (Status.REFUSED, Status.UNAVAILABLE, Status.UNSUPPORTED)


@dataclass(frozen=True)
class LaunchTerms:
    account: str
    cap: int
    ttl_ms: int
    project_ids: tuple[str, ...]
    brain_id: str


@dataclass(frozen=True)
class Launch:
    agent: AgentRecord
    grant: str
    slot: Slot | None
    outcome: Outcome


class DistributedLaunch:
    def __init__(
        self,
        controller: Controller,
        grants: LaunchAuthority,
        capacity: AccountCapacity,
        fleet: FleetRegistry,
        router: RuntimeRouter,
    ) -> None:
        self.controller, self.grants, self.capacity = controller, grants, capacity
        self.fleet, self.router = fleet, router
        self.slug = capacity.slug

    def spawn(self, request: SpawnRequest, agent: AgentRecord, terms: LaunchTerms, previous: str = "") -> Launch:
        admitted = replace(self.controller.admit(agent, previous), account=terms.account)
        grant = self.grants.issue(
            self.slug,
            admitted.execution_id,
            project_ids=list(terms.project_ids),
            brain_id=terms.brain_id,
            account=terms.account,
        )
        backend = self.router.spawn_backend()
        try:
            slot = self.capacity.reserve(grant, terms.cap, terms.ttl_ms)
        except SwarmError as error:
            return Launch(admitted, grant, None, Outcome("spawn", Status.REFUSED, backend, detail=str(error)))
        identity = {"launch_grant": grant, "execution_id": admitted.execution_id, "generation": admitted.generation}
        outcome = self.router.spawn(replace(request, task={**request.task, **identity}))
        if outcome.status in NOT_LAUNCHED:
            self.capacity.release(grant)
        return Launch(admitted, grant, slot, outcome)

    def registered(self, session: Session, grant: str) -> Slot:
        body = {"execution_id": session.execution_id, "generation": session.generation}
        self.grants.register(self.slug, grant, body)
        record = self.fleet.register(session, grant)
        return self.capacity.occupy(grant, record.scope, record.session_id)

    def exited(self, agent: AgentRecord) -> Slot | None:
        return self.capacity.end(agent.account, agent.seat, agent.execution_id, agent.generation)
