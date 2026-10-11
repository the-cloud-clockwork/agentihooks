"""The capacity and registry given here must authorize with LaunchAuthority.verify, never register."""

import json
import subprocess
import sys
from collections.abc import Callable
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Protocol

from scripts.swarm.store import AgentRecord, SwarmError
from scripts.swarm_v2 import broadcast_bridge
from scripts.swarm_v2.accounts import AccountCapacity, Slot
from scripts.swarm_v2.auth_context import LaunchAuthority
from scripts.swarm_v2.controller import Controller
from scripts.swarm_v2.hand import HANDED, Hand
from scripts.swarm_v2.registry import FleetRegistry, Session
from scripts.swarm_v2.runtime.base import Outcome, RuntimeRouter, SpawnRequest, Status

NOT_LAUNCHED = (Status.REFUSED, Status.UNAVAILABLE, Status.UNSUPPORTED)
GRANT_SECONDS = 30
NO_API_URL = "the swarm config has no API address"
NOT_HANDED = "launch grant not handed: "
HANDS = "launch-grant-hands"
Target = Callable[[SpawnRequest], dict]


class WorkerHomes(Protocol):
    def hand(self, agent: AgentRecord, grant: str) -> Hand: ...


@dataclass(frozen=True)
class WorkerHomeCommand:
    """The grant must never reach argv: the process table shows it."""

    root: Path

    def hand(self, agent: AgentRecord, grant: str) -> Hand:
        attempt = self.root / agent.execution_id
        command = [sys.executable, "-m", "scripts.swarm_v2.worker_home", "grant", str(attempt)]
        try:
            done = subprocess.run(command, input=grant, capture_output=True, text=True, timeout=GRANT_SECONDS)
        except (OSError, subprocess.TimeoutExpired):
            return Hand(False, "grant_command_unavailable")
        return HANDED if done.returncode == 0 else Hand(False, "grant_command_failed")


@dataclass(frozen=True)
class LaunchTerms:
    account: str
    cap: int
    ttl_ms: int
    project_ids: tuple[str, ...]
    brain_id: str
    api_url: str


@dataclass(frozen=True)
class Launch:
    agent: AgentRecord
    grant: str = field(repr=False)
    slot: Slot | None
    outcome: Outcome
    hand: Hand | None = None


class DistributedLaunch:
    def __init__(
        self,
        controller: Controller,
        grants: LaunchAuthority,
        capacity: AccountCapacity,
        fleet: FleetRegistry,
        router: RuntimeRouter,
        homes: WorkerHomes,
    ) -> None:
        self.controller, self.grants, self.capacity = controller, grants, capacity
        self.fleet, self.router, self.homes = fleet, router, homes
        self.slug = capacity.slug

    def spawn(self, request: SpawnRequest, agent: AgentRecord, terms: LaunchTerms, previous: str) -> Launch:
        admitted = self.controller.admit(replace(agent, account=terms.account), previous)
        grant = self.grants.issue(
            self.slug,
            admitted.execution_id,
            project_ids=list(terms.project_ids),
            brain_id=terms.brain_id,
            account=terms.account,
        )
        backend = self.router.spawn_backend(request)
        try:
            slot = self.capacity.reserve(grant, terms.cap, terms.ttl_ms)
        except SwarmError as error:
            return Launch(admitted, grant, None, Outcome("spawn", Status.REFUSED, backend, detail=str(error)))
        identity = {
            "launch_grant": grant,
            "execution_id": admitted.execution_id,
            "generation": admitted.generation,
            "endpoints": {broadcast_bridge.API_URL: terms.api_url},
        }
        outcome = self.router.spawn(replace(request, task={**request.task, **identity}))
        if outcome.status in NOT_LAUNCHED:
            self.exited(admitted)
            return Launch(admitted, grant, slot, outcome)
        hand = self.homes.hand(admitted, grant)
        store = self.controller.store
        store.redis.hset(store.key(self.slug, HANDS), admitted.execution_id, json.dumps(asdict(hand)))
        if hand.handed or outcome.status is Status.AMBIGUOUS:
            return Launch(admitted, grant, slot, outcome, hand)
        self.exited(admitted)
        self.grants.withdraw(self.slug, grant)
        return Launch(
            admitted, grant, slot, Outcome("spawn", Status.REFUSED, backend, detail=NOT_HANDED + hand.reason), hand
        )

    def from_tick(self, request: SpawnRequest, terms: LaunchTerms, target: Target) -> Outcome:
        api_url, backend = request.config.api_url, self.router.spawn_backend(request)
        if not api_url:
            return Outcome("spawn", Status.REFUSED, backend, detail=NO_API_URL)
        agent = AgentRecord(
            request.name,
            request.lane,
            request.task["id"],
            seat=request.task["seat"],
            runtime_backend=backend,
            runtime_target=target(request),
        )
        return self.spawn(request, agent, replace(terms, api_url=api_url), "").outcome

    def registered(self, session: Session, grant: str) -> Slot:
        body = {"execution_id": session.execution_id, "generation": session.generation}
        self.grants.register(self.slug, grant, body)
        record = self.fleet.register(session, grant)
        try:
            return self.capacity.occupy(grant, record.scope, record.session_id)
        except SwarmError:
            self.fleet.close(record.scope, record.session_id, grant)
            raise

    def exited(self, agent: AgentRecord) -> Slot | None:
        return self.capacity.end(agent.account, agent.seat, agent.execution_id, agent.generation)
