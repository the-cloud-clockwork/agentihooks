"""Which tasks a lane may claim, offered to the lane's work queue once per claim generation."""

from collections.abc import Callable
from dataclasses import dataclass

from scripts.events import subjects
from scripts.events.bus import Bus, Delivery, WorkQueue
from scripts.events.config import QueuePolicy
from scripts.events.envelope import Envelope
from scripts.swarm import claim_hold

AUTHOR = "readiness"


@dataclass(frozen=True)
class Snapshot:
    slug: str
    rows: dict
    doc: dict
    graph: dict
    fix: str
    claimed: Callable[[str], bool]
    admits: Callable[[dict], bool] = lambda task: True


def ready(task: dict, rows: dict, doc: dict, graph: dict, fix: str) -> bool:
    return (
        task.get("state") == "open"
        and not task.get("out_of_scope")
        and not task.get("merged_into")
        and not claim_hold.hold(task, rows, doc, graph, fix)
    )


def claimable(task: dict, lane: str, view: Snapshot) -> bool:
    return (
        task.get("lane") == lane
        and ready(task, view.rows, view.doc, view.graph, view.fix)
        and not view.claimed(task["id"])
        and view.admits(task)
    )


def generation(task: dict) -> int:
    return task.get("claim_generation") or 0


def offer_id(slug: str, task: dict) -> str:
    return f"ready:{slug}:{task['id']}:{generation(task)}"


def queue_name(slug: str, lane: str) -> str:
    return subjects.subject(slug, "ready", lane)


def offer(task: dict, view: Snapshot, now_ms: int) -> Envelope:
    return Envelope(
        id=offer_id(view.slug, task),
        type="task.ready",
        swarm=view.slug,
        entity="task",
        key=task["id"],
        version=generation(task),
        time=now_ms,
        author=AUTHOR,
        data={"lane": task["lane"], "generation": generation(task)},
    )


def offers(view: Snapshot, now_ms: int) -> list[Envelope]:
    return [
        offer(task, view, now_ms)
        for task in view.rows.values()
        if task.get("lane") and claimable(task, task["lane"], view)
    ]


def publish(bus: Bus, view: Snapshot, now_ms: int, policy: QueuePolicy = QueuePolicy()) -> list[str]:
    placed = []
    for event in offers(view, now_ms):
        if bus.queue(queue_name(view.slug, event.data["lane"]), policy).offer(event):
            placed.append(event.id)
    return placed


def withdrawal(event: Envelope, view: Snapshot) -> str:
    task = view.rows.get(event.key)
    if task is None:
        return "the task is gone"
    if view.claimed(task["id"]):
        return "it is claimed"
    if task.get("state") != "open":
        return f"it is {task.get('state')}"
    if generation(task) != event.version:
        return f"it is at generation {generation(task)}"
    if task.get("lane") != event.data["lane"]:
        return f"it moved to lane {task.get('lane')}"
    if not ready(task, view.rows, view.doc, view.graph, view.fix):
        return claim_hold.hold(task, view.rows, view.doc, view.graph, view.fix) or "it is not ready"
    return "" if view.admits(task) else "a release or apply wait holds it"


def take(queue: WorkQueue, view: Snapshot, timeout_ms: int) -> Delivery | None:
    while (delivery := queue.take(timeout_ms)) is not None:
        if not withdrawal(delivery.event, view):
            return delivery
        delivery.term()
    return None
