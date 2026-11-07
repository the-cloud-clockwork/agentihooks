"""Attempts whose Pod the scheduler cannot place end after a bounded wait, and their tasks reopen for hive placement."""

from collections.abc import Iterable
from dataclasses import replace
from typing import TYPE_CHECKING, Any, cast

from scripts.swarm import lease
from scripts.swarm.store import AgentRecord, RedisStore, SwarmError
from scripts.swarm_v2 import retention
from scripts.swarm_v2.controller import Controller
from scripts.swarm_v2.kubernetes.failures import FENCES
from scripts.swarm_v2.kubernetes.pool import unschedulable
from scripts.swarm_v2.kubernetes.watch import BACKEND, EXECUTION_LABEL

if TYPE_CHECKING:
    from scripts.swarm_v2.kubernetes.replacement import TaskLedger

WINDOW_MS = 300_000
SINCE = "attempt-unschedulable-since"
ENDED = "ended"
CANCELLED = "cancelled"
REOPENED = {"state": "open", "claimed_by": "", "awaiting": ""}


def reopen(store: RedisStore, slug: str, controller: Controller, ledger: "TaskLedger", agent: AgentRecord) -> bool:
    controller.require()
    assert controller.held is not None
    try:
        task: dict[str, Any] = next(
            (row for row in ledger.state(slug)["tasks"] if row["id"] == agent.task), dict[str, Any]()
        )
        if task.get("state") == "claimed" and task.get("claimed_by", agent.name) in ("", agent.name):
            with lease.fencing(controller.held.epoch):
                ledger.update_task(
                    slug,
                    agent.task,
                    REOPENED,
                    if_state=("claimed",),
                    if_match={"claimed_by": task.get("claimed_by", "")},
                )
    except SwarmError as error:
        print(f"controller: the ledger refused to reopen task {agent.task}: {error}", flush=True)
        return False
    controller.require()
    cast(Any, store).put_agent(slug, replace(store.execution(slug, agent.execution_id), state=ENDED))
    return True


class Unschedulable:
    """A live Kubernetes attempt whose Pod stays unschedulable for `window_ms` is finalized cancelled, so completion
    cleanup deletes its Pod, and its task reopens."""

    def __init__(
        self,
        store: RedisStore,
        slug: str,
        controller: Controller,
        ledger: "TaskLedger",
        window_ms: int = WINDOW_MS,
    ) -> None:
        self.store, self.slug, self.controller, self.ledger = store, slug, controller, ledger
        self.window_ms = window_ms
        self.redis: Any = cast(Any, store).redis

    def __call__(self, pods: Iterable[dict[str, Any]]) -> dict[str, str]:
        stuck = {pod["metadata"].get("labels", {}).get(EXECUTION_LABEL) for pod in pods if unschedulable(pod)}
        key, now = self.store.key(self.slug, SINCE), lease.now_ms(self.store)
        waiting: set[str] = set()
        ended: dict[str, str] = {}
        for agent in self.store.execution_registry.records(self.slug):
            finishing = agent.state == ENDED and self.redis.hexists(key, agent.execution_id)
            if not self._live(agent) or (agent.execution_id not in stuck and not finishing):
                continue
            self.controller.require()
            self.redis.hsetnx(key, agent.execution_id, now)
            if now - int(self.redis.hget(key, agent.execution_id)) >= self.window_ms and self._end(agent):
                ended[agent.execution_id] = ENDED
            else:
                waiting.add(agent.execution_id)
        settled = set(self.redis.hkeys(key)) - waiting
        if settled:
            self.controller.require()
            self.redis.hdel(key, *sorted(settled))
        return ended

    def _live(self, agent: AgentRecord) -> bool:
        return (
            agent.runtime_backend == BACKEND
            and not self.redis.hexists(self.store.key(self.slug, FENCES), agent.execution_id)
            and retention.final(self.store, self.slug, agent.execution_id) is None
        )

    def _end(self, agent: AgentRecord) -> bool:
        if agent.state != ENDED and not reopen(self.store, self.slug, self.controller, self.ledger, agent):
            return False
        end = retention.archive_watermark(self.store, self.slug, agent.execution_id)
        outcome = f"{SINCE}/{agent.execution_id}"
        retention.finalize(self.store, self.slug, self.controller.require, agent.execution_id, CANCELLED, end, outcome)
        return True
