"""The launch an expert seat is held to: Claude Code on the Fable model at effort xhigh or max.

An expert lineage runs one harness and one model whatever the swarm, a lane setting, a saved handoff launch or a
recorded session says. Its effort is the lineage's own: xhigh unless the operator chose max when it started.
"""

import json
from dataclasses import replace
from typing import TYPE_CHECKING, Any

from scripts.swarm import session_model

if TYPE_CHECKING:
    from scripts.swarm.model_pick import ModelPick
    from scripts.swarm_v2.runtime.base import Outcome

LANE = "expert"
HARNESS = "claude"
MODEL = "fable"
EFFORTS = ("xhigh", "max")
DEFAULT_EFFORT = EFFORTS[0]
EFFORT_KEY = "expert_effort"
SOURCE = "expert"


def pin(lane: str, picked: "ModelPick", harness: str, effort: str | None) -> "ModelPick":
    """The pick an expert launches with, whatever was picked, saved or recorded: Fable at the effort asked for when
    that is xhigh or max and at xhigh otherwise. Another lane's pick comes back unchanged, and an expert launch on
    another harness is refused."""
    if lane != LANE:
        return picked
    if harness != HARNESS:
        from scripts.swarm.tick import SpawnError

        raise SpawnError(f"an expert launches on {HARNESS}, never on {harness or 'no harness'}", "unsupported")
    held = effort if effort in EFFORTS else DEFAULT_EFFORT
    return replace(picked, model=MODEL, effort=held, source=SOURCE, confidence=None)


def on_pin(harness: str, model: str, effort: str) -> bool:
    """Whether a recorded launch is the expert pin, a full Fable model name counting as the Fable alias."""
    return harness == HARNESS and session_model.same(MODEL, model) and effort in EFFORTS


def acknowledging(slug: str, store: Any, agent: Any) -> bool:
    from scripts.hive.launch import ORDERS

    raw = store.redis.hget(store.key(slug, ORDERS), agent.task)
    return bool(raw) and json.loads(raw)["execution_id"] == agent.execution_id


def collect(runtime: Any, agent: Any) -> "Outcome | None":
    from scripts.hive import launch, orders
    from scripts.swarm.tick import SpawnError
    from scripts.swarm_v2.runtime.base import Status

    hive = getattr(runtime, "hive_launch", None)
    if hive is None or (order := hive.orders_for(agent.task)) is None:
        return None
    if order.execution_id != agent.execution_id:
        return None
    if hive.stale(order):
        ack = hive.orders.ack(order)
        if ack is not None and ack.status == "live":
            hive.reap_execution(order)
        else:
            hive.reap(order, agent)
        hive.end(agent, f"hive launch failed: {orders.STALE_EPOCH}")
        raise SpawnError(orders.STALE_EPOCH)
    if hive.unacknowledged(order):
        hive.retire(order, agent)
        raise SpawnError(launch.EXPIRED)
    outcome = hive.poll(order)
    if outcome.status == Status.REFUSED:
        raise SpawnError(outcome.detail)
    return outcome
