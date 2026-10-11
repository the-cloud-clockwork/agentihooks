"""A fenced attempt's replacement launches from the checkpoint its recovery decision names; until it launches, the
attempt's task waits on recovery on the ledger."""

from collections.abc import Callable
from typing import TYPE_CHECKING, Protocol

from scripts.swarm import lease
from scripts.swarm.store import AgentRecord, SwarmError
from scripts.swarm_v2.kubernetes.failures import FENCES, PENDING, Decision, Recovery
from scripts.swarm_v2.runtime.base import SpawnRequest, Status

if TYPE_CHECKING:
    from scripts.swarm_v2.launch import Launch

LAUNCHED = "attempt-replacement-launches"
MARKED = "recovery-pending-tasks"
CLEARED = ""
SUPERSEDED = "superseded"
STARTED = (Status.OK, Status.AMBIGUOUS)
MARKABLE = ("claimed",)


class TaskLedger(Protocol):
    def update_task(self, slug: str, task_id: str, fields: dict, if_state: tuple = ()) -> dict: ...


Relaunch = Callable[[SpawnRequest, AgentRecord], "Launch"]


class Replacements:
    def __init__(self, recovery: Recovery, relaunch: Relaunch, ledger: TaskLedger) -> None:
        self.recovery, self.relaunch, self.ledger = recovery, relaunch, ledger
        self.store, self.slug, self.controller = recovery.store, recovery.slug, recovery.controller

    def __call__(self) -> dict[str, str]:
        launched = {}
        for execution_id in self._unlaunched():
            decision = self.recovery.decision(execution_id)
            if decision is not None and decision.replacement:
                launched[execution_id] = self._launch(decision)
        self._mark()
        return launched

    def _key(self, name: str) -> str:
        return self.store.key(self.slug, name)

    def _unlaunched(self) -> list[str]:
        fenced = set(self.store.redis.hkeys(self._key(FENCES)))
        return sorted(fenced - set(self.store.redis.hkeys(self._key(LAUNCHED))))

    def _launch(self, decision: Decision) -> str:
        successor = self.store.execution(self.slug, decision.replacement)
        occupant = self.store.execution_occupants(self.slug)[successor.seat]
        self.controller.require()
        if occupant.execution_id != successor.execution_id:
            self._launched(decision)
            return SUPERSEDED
        task = {
            "id": successor.task,
            "seat": successor.seat,
            "profile": successor.profile,
            "controller_epoch": self.controller.held.epoch,
            "checkpoint": decision.checkpoint,
        }
        request = SpawnRequest(self.store.config(self.slug), successor.lane, successor.name, task)
        outcome = self.relaunch(request, successor).outcome
        if outcome.status in STARTED:
            self._launched(decision)
        return outcome.status.value

    def _launched(self, decision: Decision) -> None:
        self.controller.require()
        self.store.redis.hset(self._key(LAUNCHED), decision.execution_id, decision.replacement)

    def _mark(self) -> None:
        waiting = {self.store.execution(self.slug, execution_id).task for execution_id in self._unlaunched()}
        marked = set(self.store.redis.hkeys(self._key(MARKED)))
        for task in sorted(waiting - marked):
            self._await(task, PENDING)
        for task in sorted(marked - waiting):
            self._await(task, CLEARED)

    def _await(self, task: str, state: str) -> None:
        self.controller.require()
        try:
            with lease.fencing(self.controller.held.epoch):
                self.ledger.update_task(self.slug, task, {"awaiting": state}, if_state=MARKABLE)
        except SwarmError as error:
            print(f"controller: the ledger refused the recovery mark on task {task}: {error}", flush=True)
            return
        if state:
            self.store.redis.hset(self._key(MARKED), task, state)
        else:
            self.store.redis.hdel(self._key(MARKED), task)
