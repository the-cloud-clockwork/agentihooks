"""A fenced attempt's replacement launches from the checkpoint its recovery decision names; until it launches, the
attempt's task waits on recovery on the ledger."""

from collections.abc import Callable
from functools import partial
from typing import TYPE_CHECKING, Protocol

from scripts.swarm import lease
from scripts.swarm.store import AgentRecord, SwarmError
from scripts.swarm_v2.kubernetes.failures import FENCES, PENDING, Decision, Recovery
from scripts.swarm_v2.runtime.base import SpawnRequest, Status

if TYPE_CHECKING:
    from scripts.swarm_v2.launch import Launch

LAUNCHED = "attempt-replacement-launches"
MARKED = "recovery-pending-tasks"
RECOVERY = "recovery"
RECORDED = "attempt-recovery-records"
CLEARED = ""
SUPERSEDED = "superseded"
STARTED = (Status.OK, Status.AMBIGUOUS)


class TaskLedger(Protocol):
    def state(self, slug: str) -> dict: ...

    def update_task(self, slug: str, task_id: str, fields: dict) -> dict: ...


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
        self._record()
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
            self.store.redis.hset(self._key(RECORDED), decision.execution_id, SUPERSEDED)
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

    def _record(self) -> None:
        for execution_id, successor in sorted(self.store.redis.hgetall(self._key(LAUNCHED)).items()):
            agent = self.store.execution(self.slug, successor)
            if self.store.execution_occupants(self.slug)[agent.seat].execution_id != successor:
                self.store.redis.hset(self._key(RECORDED), execution_id, SUPERSEDED)
                continue
            outcome = self.recovery.outcome(successor)
            recorded = self.store.redis.hget(self._key(RECORDED), execution_id)
            if recorded == SUPERSEDED or recorded == "verified" or (recorded == "accepted" and outcome is None):
                continue
            directive = self.recovery.restore(successor)
            if directive is not None:
                value = outcome or {**directive, "status": "accepted"}
                task = self.store.execution(self.slug, successor).task
                self._ledgered(task, partial(self._recovered, execution_id, value))

    def _recovered(self, execution_id: str, restore: dict, task: str) -> None:
        self.ledger.update_task(self.slug, task, {RECOVERY: restore})
        self.store.redis.hset(self._key(RECORDED), execution_id, restore["status"])

    def _launched(self, decision: Decision) -> None:
        self.controller.require()
        self.store.redis.hset(self._key(LAUNCHED), decision.execution_id, decision.replacement)

    def _mark(self) -> None:
        waiting = {self.store.execution(self.slug, execution_id).task for execution_id in self._unlaunched()}
        for execution_id, successor in self.store.redis.hgetall(self._key(LAUNCHED)).items():
            if (
                self.store.redis.hget(self._key(RECORDED), execution_id) != SUPERSEDED
                and self.recovery.outcome(successor) is None
            ):
                waiting.add(self.store.execution(self.slug, successor).task)
        marked = set(self.store.redis.hkeys(self._key(MARKED)))
        for task in sorted(waiting - marked):
            self._ledgered(task, self._hold)
        for task in sorted(marked - waiting):
            self._ledgered(task, self._release)

    def _ledgered(self, task: str, write: Callable[[str], None]) -> None:
        self.controller.require()
        try:
            with lease.fencing(self.controller.held.epoch):
                write(task)
        except SwarmError as error:
            print(f"controller: the ledger refused the recovery mark on task {task}: {error}", flush=True)

    def _hold(self, task: str) -> None:
        """The task's own awaiting value is kept, so the operator's merge approval prompt comes back after recovery."""
        rows = {row["id"]: row.get("awaiting", CLEARED) for row in self.ledger.state(self.slug)["tasks"]}
        prior = rows.get(task, CLEARED)
        self.ledger.update_task(self.slug, task, {"awaiting": PENDING})
        self.store.redis.hset(self._key(MARKED), task, CLEARED if prior == PENDING else prior)

    def _release(self, task: str) -> None:
        prior = self.store.redis.hget(self._key(MARKED), task)
        self.ledger.update_task(self.slug, task, {"awaiting": prior})
        self.store.redis.hdel(self._key(MARKED), task)
