"""Place a scheduled workflow run on a live hive that lists its project and declares its needs, or record its wait."""

import json
import time
from collections.abc import Mapping
from dataclasses import dataclass, replace
from typing import Any, Protocol

from scripts.hive import run_orders
from scripts.hive.placement import YES, Wait, repository
from scripts.hive.registry import live
from scripts.swarm.probe import ROOT

WAITS = f"{ROOT}:run-placement-waits"
NO_HOST = "no live hive lists the repo {repo}"
NO_NEEDS = "no live hive that lists the repo {repo} declares {needs}"
EXPIRY_S = 3600


@dataclass(frozen=True)
class Run:
    delivery_id: str
    workflow: str
    case: str
    project: str
    owner: str
    needs: tuple[str, ...]


class Orders(Protocol):
    def running(self, hive_id: str) -> int: ...

    def send(self, hive_id: str, run: Run) -> object: ...


def choose(
    hives: list[dict[str, Any]], repo: str, needs: tuple[str, ...], running: Mapping[str, int], now_ms: int
) -> str | Wait:
    """The fitting hive with the fewest running runs, ties by id, or the named reason none fits."""
    hosting = [hive for hive in hives if live(hive, now_ms) and hive.get("draining") != YES and repository(hive, repo)]
    if not hosting:
        return Wait(NO_HOST.format(repo=repo))
    fitting = [hive for hive in hosting if set(needs) <= set(hive.get("capabilities") or ())]
    if not fitting:
        return Wait(NO_NEEDS.format(repo=repo, needs=", ".join(needs)))
    return min(fitting, key=lambda hive: (running.get(hive["id"], 0), hive["id"]))["id"]


def waits(redis: Any) -> dict[str, dict[str, Any]]:
    """Every recorded run wait by delivery id."""
    return {delivery: json.loads(raw) for delivery, raw in redis.hgetall(WAITS).items()}


def deliver(redis: Any, run: Run, hives: list[dict[str, Any]], orders: Orders, now_ms: int) -> str | Wait:
    """Send the run to its chosen hive, or record its wait and send nothing."""
    running = {hive["id"]: orders.running(hive["id"]) for hive in hives}
    picked = choose(hives, run.project, run.needs, running, now_ms)
    if isinstance(picked, Wait):
        row = {"workflow": run.workflow, "project": run.project, "reason": picked.reason, "seen_at": now_ms}
        redis.hset(WAITS, run.delivery_id, json.dumps(row))
        return picked
    hive = next(hive for hive in hives if hive["id"] == picked)
    orders.send(picked, replace(run, project=repository(hive, run.project)))
    redis.hdel(WAITS, run.delivery_id)
    return picked


class RunOrderSender:
    """Orders over the hive run order store: running runs are the unfinished entries on a hive's stream."""

    def __init__(self, redis: Any, now_s: float) -> None:
        self.redis = redis
        self.now_s = now_s
        self.store = run_orders.RunOrders(redis)

    def running(self, hive_id: str) -> int:
        return int(self.redis.xlen(self.store.stream(hive_id)))

    def send(self, hive_id: str, run: Run) -> object:
        order = run_orders.RunOrder(
            run.delivery_id,
            run.workflow,
            run.case,
            run.project,
            run.owner,
            run.needs,
            hive_id,
            int(self.now_s) + EXPIRY_S,
        )
        return self.store.send(hive_id, order)


def dispatch(run: Run, redis: Any) -> str | Wait:
    """Place one scheduled run against the live hive registry."""
    from scripts.hive import registry

    now_s = time.time()
    return deliver(redis, run, registry.hives(redis), RunOrderSender(redis, now_s), int(now_s * 1000))
