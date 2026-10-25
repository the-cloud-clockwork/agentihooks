"""The capacity and registry given here must authorize with LaunchAuthority.verify, never register."""

import json
import subprocess
import sys
from collections.abc import Callable
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Protocol

from hooks.context import profile_chain
from scripts.swarm import live_binding, profile_choice, prompt
from scripts.swarm.store import AgentRecord, SwarmError
from scripts.swarm_ledger import hosted_probe
from scripts.swarm_v2 import broadcast_bridge, worker_home
from scripts.swarm_v2.accounts import ACCOUNT_FULL, AccountCapacity, Slot
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
ENDINGS = "launch-endings"
ENDED = "ended"
WORKER_DEFAULT = "default"
NO_ACCOUNT = "no approved account has quota and a free launch slot"
Target = Callable[[SpawnRequest], dict]
AccountChooser = Callable[[int, str | None, tuple[str, str] | None], str | None]
PlannedAccount = Callable[[str], str | None]
Ending = Callable[[AgentRecord, str], AgentRecord]


class WorkerHomes(Protocol):
    def hand(self, agent: AgentRecord, grant: str, opening: str) -> Hand: ...


@dataclass(frozen=True)
class WorkerHomeCommand:
    """The grant must never reach argv: the process table shows it."""

    root: Path

    def hand(self, agent: AgentRecord, grant: str, opening: str = "") -> Hand:
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
    bundled: bool = False


@dataclass(frozen=True)
class Launch:
    agent: AgentRecord
    grant: str = field(repr=False)
    slot: Slot | None
    outcome: Outcome
    hand: Hand | None = None


def seat_profile(request: SpawnRequest) -> str:
    task, lane = request.task, request.config.lanes.get(request.lane, {})
    relaunch = live_binding.complete(task.get("launch_assignment"))
    saved = relaunch or profile_choice.transfer(task)
    decision = profile_choice.kept(task, saved, relaunch) or profile_choice.pick(
        request.config.slug, request.lane, lane, task
    )
    return decision.profile


def opening(request: SpawnRequest) -> str:
    config = request.config
    role = config.lanes.get(request.lane, {}).get("role", "")
    return prompt.build(
        config.slug, config.repo, request.lane, request.name, request.task, role=role, autonomy=config.autonomy
    )


def worker_profiles() -> set[str]:
    roles = {
        path.name for path in profile_chain.PACKAGE_ROLES.iterdir() if path.is_dir() and not path.name.startswith("_")
    }
    return {WORKER_DEFAULT, *roles}


def shipped(profile: str, bundled: bool) -> bool:
    return profile in worker_profiles() or (bundled and worker_home.NAME.fullmatch(profile) is not None)


def quota_predecessor(task: dict) -> tuple[str, str] | None:
    """The account a quota handoff leaves, read as ``HerdrRuntime.quota_requirements`` reads it."""
    envelope = task.get("handoff_envelope") or {}
    saved = task.get("launch_assignment") or envelope.get("launch") or {}
    if envelope.get("reason") != "quota" or not saved.get("harness") or not saved.get("account"):
        return None
    return saved["harness"], saved["account"]


def hosted_ledger() -> dict[str, str]:
    url = hosted_probe.address()
    return {hosted_probe.HOSTED_ENV: url} if url else {}


def kept(agent: AgentRecord, reason: str) -> AgentRecord:
    return agent


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
        from scripts.routing.reservation import LaunchSlots

        self.slug = capacity.slug
        self.launch_slots = LaunchSlots(controller.store.redis)
        self.accounts: AccountChooser | None = None
        self.planned: PlannedAccount | None = None
        self.opening: Callable[[SpawnRequest], str] = opening

    def spawn(
        self, request: SpawnRequest, agent: AgentRecord, terms: LaunchTerms, previous: str, opening: str
    ) -> Launch:
        admitted = self.controller.admit(replace(agent, account=terms.account), previous)
        return self._place(request, admitted, terms, self.end_refused, opening)

    def relaunch(self, request: SpawnRequest, admitted: AgentRecord, terms: LaunchTerms) -> Launch:
        """A refused relaunch leaves its replacement unended, so the next recovery pass places it again."""
        opening = "" if request.task.get("checkpoint") else self.opening(request)
        return self._place(request, admitted, replace(terms, account=admitted.account), kept, opening)

    def _place(
        self, request: SpawnRequest, admitted: AgentRecord, terms: LaunchTerms, refused: Ending, opening: str
    ) -> Launch:
        grant = self.grants.issue(
            self.slug,
            admitted.execution_id,
            project_ids=list(terms.project_ids),
            brain_id=terms.brain_id,
            account=terms.account,
        )
        backend = self.router.spawn_backend(request)
        try:
            slot = self._reserve(admitted, terms, grant)
        except SwarmError as error:
            self.launch_slots.release(admitted.execution_id)
            ended = refused(admitted, str(error))
            self.grants.withdraw(self.slug, grant)
            status = Status.UNAVAILABLE if str(error) == ACCOUNT_FULL else Status.REFUSED
            return Launch(ended, grant, None, Outcome("spawn", status, backend, detail=str(error)))
        identity = {
            "launch_grant": grant,
            "execution_id": admitted.execution_id,
            "generation": admitted.generation,
            "account": terms.account,
            "profile": admitted.profile,
            "endpoints": {broadcast_bridge.API_URL: terms.api_url, **hosted_ledger()},
        }
        outcome = self.router.spawn(replace(request, task={**request.task, **identity}))
        if outcome.status in NOT_LAUNCHED:
            self.exited(admitted)
            return Launch(admitted, grant, slot, outcome)
        hand = self.homes.hand(admitted, grant, opening)
        store = self.controller.store
        store.redis.hset(store.key(self.slug, HANDS), admitted.execution_id, json.dumps(asdict(hand)))
        if hand.handed or outcome.status is Status.AMBIGUOUS:
            return Launch(admitted, grant, slot, outcome, hand)
        self.exited(admitted)
        self.grants.withdraw(self.slug, grant)
        return Launch(
            admitted, grant, slot, Outcome("spawn", Status.REFUSED, backend, detail=NOT_HANDED + hand.reason), hand
        )

    def _reserve(self, admitted: AgentRecord, terms: LaunchTerms, grant: str) -> Slot:
        from scripts.swarm_v2.accounts import CONFLICTS, RESERVED

        held = self.capacity.rows(terms.account).get(f"{self.slug}/{admitted.seat}")
        previous = (
            held.execution_id if held and held.generation < admitted.generation and held.state == RESERVED else ""
        )
        if not self.launch_slots.reserve(
            f"{admitted.harness or 'claude'}:{terms.account}", admitted.execution_id, terms.cap, terms.ttl_ms, previous
        ):
            self.controller.store.redis.hincrby(CONFLICTS, terms.account)
            raise SwarmError(ACCOUNT_FULL)
        return self.capacity.reserve(grant, terms.cap, terms.ttl_ms)

    def from_tick(self, request: SpawnRequest, terms: LaunchTerms, target: Target) -> Outcome:
        api_url, backend = request.config.api_url, self.router.spawn_backend(request)
        if not api_url:
            return Outcome("spawn", Status.REFUSED, backend, detail=NO_API_URL)
        profile = seat_profile(request)
        if self.router.placement is not None and profile in self.router.placement.local_profiles:
            return Outcome("spawn", Status.REFUSED, backend, detail=f"profile {profile} runs only on the workstation")
        if not shipped(profile, terms.bundled):
            return Outcome(
                "spawn", Status.REFUSED, backend, detail=f"profile {profile} is not shipped in the worker image"
            )
        if self.accounts is not None:
            planned = None if self.planned is None else self.planned(request.task["id"])
            account = self.accounts(terms.cap, planned, quota_predecessor(request.task))
            if account is None:
                return Outcome("spawn", Status.UNAVAILABLE, backend, detail=NO_ACCOUNT)
            terms = replace(terms, account=account)
        agent = AgentRecord(
            request.name,
            request.lane,
            request.task["id"],
            seat=request.task["seat"],
            runtime_backend=backend,
            runtime_target=target(request),
            profile=profile,
            harness=request.task.get("harness", "claude"),
        )
        store = self.controller.store
        occupant = store.execution_occupants(self.slug).get(agent.seat)
        refused = occupant and store.redis.hexists(store.key(self.slug, ENDINGS), occupant.execution_id)
        previous = occupant.execution_id if refused else ""
        return self.spawn(request, agent, replace(terms, api_url=api_url), previous, self.opening(request)).outcome

    def end_refused(self, agent: AgentRecord, reason: str) -> AgentRecord:
        store, ended = self.controller.store, replace(agent, state=ENDED)
        self.controller.require()
        ending = {"reason": reason, "generation": agent.generation}
        store.redis.hset(store.key(self.slug, ENDINGS), agent.execution_id, json.dumps(ending))
        store.put_agent(self.slug, ended)
        return ended

    def registered(self, session: Session, grant: str) -> Slot:
        body = {"execution_id": session.execution_id, "generation": session.generation}
        self.grants.register(self.slug, grant, body)
        record = self.fleet.register(session, grant)
        try:
            slot = self.capacity.occupy(grant, record.scope, record.session_id)
            if not self.launch_slots.acknowledge(record.execution_id):
                raise SwarmError("reservation_expired")
            return slot
        except SwarmError:
            self.fleet.close(record.scope, record.session_id, grant)
            raise

    def exited(self, agent: AgentRecord) -> Slot | None:
        return self.capacity.end(agent.account, agent.seat, agent.execution_id, agent.generation)
