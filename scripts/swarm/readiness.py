"""Which tasks a lane may claim, offered to the lane's work queue on every hub pass.

An offer's id carries the task's claim generation, so the bus drops a repeat only inside its dedupe window.
"""

import threading
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from functools import cached_property
from typing import Any, NamedTuple

from scripts.events import subjects
from scripts.events.bus import Bus, Delivery, WorkQueue
from scripts.events.config import BusConfig, QueuePolicy
from scripts.events.envelope import Envelope
from scripts.events.scope import LANES
from scripts.swarm import apply_wait, claim_hold, fleet_red, freeze, offer_quota, release_wait
from scripts.swarm.ledger_client import LedgerClient
from scripts.swarm.probe import ROOT
from scripts.swarm.store import EVENTS_ENGINE, RedisStore, SwarmError, connect
from scripts.swarm_ledger.repository import hierarchy

AUTHOR = "readiness"
UNFIT_NAK_MS = 5_000
PREFERRED, ORDERED, REST = range(3)
LAST: tuple[int, ...] = (REST + 1,)


class Classes(NamedTuple):
    """The focus rank of each task of a snapshot, the rank of a task no focus covers, and the one function that gives
    a task's offer its class with the keys that order offers inside it."""

    focus: dict[str, int]
    unfocused: int
    band: Callable[[dict[str, Any]], tuple[int, ...]]


@dataclass(frozen=True)
class Snapshot:
    slug: str
    rows: dict[str, Any]
    doc: dict[str, Any]
    graph: dict
    fix: str
    claimed: Callable[[str], bool]
    admits: Callable[[dict], bool]

    @cached_property
    def classes(self) -> Classes:
        """The classes of the snapshot's offers, worked out once for every read of the snapshot."""
        return _classes(self.doc)

    @cached_property
    def admitted(self) -> frozenset[str] | None:
        """The focus queue of the snapshot's document, worked out once for every claimable check over it."""
        return freeze.admitted(self.doc, self.graph)


def _classes(doc: dict[str, Any]) -> Classes:
    """Class each task of the document: urgent or under a focus first, then under an operator order, oldest order
    first, then the rest, each by ledger rank band."""
    from scripts.swarm import focus_budget, orders, preempt
    from scripts.swarm_ledger import ledger_rank

    focus, unfocused, stamped = focus_budget.ranks(doc), focus_budget.outside(doc), orders.stamps(doc)

    def band(task: dict[str, Any]) -> tuple[int, ...]:
        rank = ledger_rank.order(task)
        if focus[task["id"]] != unfocused or task.get("rank") == preempt.URGENT:
            return PREFERRED, rank
        at = stamped.get(task["id"])
        return (REST, rank) if at is None else (ORDERED, at, rank)

    return Classes(focus, unfocused, band)


def _on_events(store: RedisStore, slug: str) -> bool:
    try:
        return store.config(slug).engine == EVENTS_ENGINE
    except SwarmError:
        return False


class Source:
    def __init__(self, store: RedisStore | None = None, ledger: LedgerClient | None = None) -> None:
        self._store = store
        self._ledger = ledger or LedgerClient(service=True)
        self._exported: dict[str, tuple[int, dict[str, Any]]] = {}
        self._fetching: dict[str, threading.Lock] = {}
        self._guard = threading.Lock()

    def slugs(self) -> list[str]:
        self._store = self._store or connect()
        found = [slug for slug in self._store.slugs() if _on_events(self._store, slug)]
        with self._guard:
            self._exported = {slug: kept for slug, kept in self._exported.items() if slug in found}
        return found

    def state(self, slug: str) -> dict[str, Any]:
        """The export of ``slug`` at the revision read while holding the swarm's lock, fetched again only when that
        revision is newer than the kept one, with task rows of the caller's own so a field a reader sets on a row never
        reaches the kept copy; values nested inside a row and the other export keys stay shared and are read only."""
        with self._guard:
            fetching = self._fetching.setdefault(slug, threading.Lock())
        with fetching:
            rev = self._ledger.metadata(slug)["_meta"]["rev"]
            kept = self._exported.get(slug)
            if kept is None or kept[0] < rev:
                kept = (rev, self._ledger.state(slug))
                with self._guard:
                    self._exported[slug] = kept
        return {**kept[1], "tasks": [dict(row) for row in kept[1]["tasks"]]}

    def snapshot(self, slug: str) -> Snapshot:
        self._store = self._store or connect()
        config = self._store.config(slug)
        doc = self.state(slug)
        rows = {task["id"]: task for task in doc["tasks"]}
        owned = {agent.task for agent in self._store.agents(slug) if agent.state != "finished"}
        released = release_wait.checker(config)
        applied = apply_wait.checker(config, rows)
        unshared = fleet_red.checker(fleet_red.saved_reader(self._store, slug))
        fix = freeze.fix_phase(config)
        lab = claim_hold.lab_reason(self._store, slug, fix)
        return Snapshot(
            slug,
            rows,
            doc,
            hierarchy.project(doc)[0],
            fix,
            lambda task_id: task_id in owned or self._store.claimant(slug, task_id) is not None,
            lambda task: (
                config.state == "running" and released(task) and applied(task) and unshared(task) and not lab(task)
            ),
        )


def ready(task: dict, rows: dict, doc: dict, graph: dict, fix: str, admitted_queue: Any = freeze.UNCACHED) -> bool:
    return (
        task.get("state") == "open"
        and not task.get("out_of_scope")
        and not task.get("merged_into")
        and not claim_hold.holds(task, rows, doc, graph, fix, admitted_queue)  # pyright: ignore[reportUnknownArgumentType]
    )


def claimable(task: dict, lane: str, view: Snapshot) -> bool:
    return (
        task.get("lane") == lane
        and ready(task, view.rows, view.doc, view.graph, view.fix, admitted_queue=view.admitted)
        and not view.claimed(task["id"])
        and view.admits(task)
    )


def generation(task: dict) -> int:
    return task.get("claim_generation") or 0


def offer_id(slug: str, task: Mapping[str, Any]) -> str:
    """A workstation task's offer carries the root hive in its id, so the offer pinned there is never deduplicated
    against the unpinned one withdrawn when the task turned workstation."""
    pin = f":{ROOT}" if task.get("workstation") else ""
    return f"ready:{slug}:{task['id']}:{generation(task)}{pin}"


def queue_name(slug: str, lane: str) -> str:
    return subjects.subject(slug, "ready", lane)


def offer(task: dict[str, Any], view: Snapshot, now_ms: int) -> Envelope:
    data: dict[str, Any] = {
        "lane": task["lane"],
        "generation": generation(task),
        "quota": offer_quota.restrictions(task),
    }
    if hive := task.get("hive") or (ROOT if task.get("workstation") else None):
        data["hive"] = hive
    if needs := task.get("needs"):
        data["needs"] = list(needs)
    return Envelope(
        id=offer_id(view.slug, task),
        type="task.ready",
        swarm=view.slug,
        entity="task",
        key=task["id"],
        version=generation(task),
        time=now_ms,
        author=AUTHOR,
        data=data,
    )


def placeable(task: Mapping[str, Any], hive: str, capabilities: Callable[[], Iterable[str]]) -> bool:
    """Only the root hive takes a workstation task, and no hive takes a task pinned to another hive or one needing what
    it lacks; its capabilities are read only for a task that names needs."""
    needs: set[str] = set(task.get("needs") or ())
    return (
        (hive == ROOT or not task.get("workstation"))
        and task.get("hive") in (None, "", hive)
        and (not needs or needs <= set(capabilities()))
    )


def root_only(task: Mapping[str, Any]) -> bool:
    """A task only the root hive takes: one pinned to it, or a workstation task pinned to no other hive."""
    return task.get("hive") == ROOT or (bool(task.get("workstation")) and not task.get("hive"))


def root_waiting(view: Snapshot, capabilities: Callable[[], Iterable[str]]) -> int:
    """The snapshot swarm's claimable tasks only the root hive takes and can place, each keeping one root slot from
    work any hive can run; root's capabilities are read only for a task that names needs."""
    rows: Any = view.rows
    return sum(
        1
        for task in rows.values()
        if root_only(task)
        and task.get("lane")
        and placeable(task, ROOT, capabilities)
        and claimable(task, task["lane"], view)
    )


def fits(event: Envelope, hive: str, capabilities: Callable[[], Iterable[str]]) -> bool:
    data: Any = event.data
    return placeable(data, hive, capabilities)


def declared(store: Any, hive: str) -> Callable[[], list[str]]:
    """The capabilities a hive registered in the store's Redis, read when called."""

    def capabilities() -> list[str]:
        from scripts.hive import registry

        record: Any = registry.show(store.redis, hive) or {}
        return list(record.get("capabilities", []))

    return capabilities


def anywhere(event: Envelope) -> bool:
    return True


def offers(view: Snapshot, now_ms: int) -> list[Envelope]:
    """Each claimable task's offer: focused and urgent tasks first, then the tasks under an operator order, oldest
    order first, then the rest, each by ledger rank band and focused tasks by focus record order."""
    from scripts.swarm import focus_budget

    rows: Any = view.rows
    ready = [
        (task["lane"], task)
        for task in rows.values()
        if task.get("lane") in LANES
        and (not task.get("workstation") or task.get("hive") in (None, "", ROOT))
        and claimable(task, task["lane"], view)
    ]
    focus, unfocused, band = view.classes
    return [offer(task, view, now_ms) for _, task in focus_budget.ordered(ready, focus, band, unfocused)]


def publish(bus: Bus, view: Snapshot, now_ms: int, policy: QueuePolicy = QueuePolicy()) -> list[str]:
    """Opens every lane queue first, so a hive polling a lane with no offer yet finds its stream provisioned, and
    withdraws the stale offers there before the fresh ones join them; an offer still queued is not offered again, so
    one that waits past the dedupe window stays a single copy, unless it waits ``behind`` an offer the hub would offer
    before it, when it is withdrawn and offered again after it."""
    events = offers(view, now_ms)
    offered = {event.id for event in events}
    lanes = dict.fromkeys([*LANES, *(event.data["lane"] for event in events)])
    queues = {lane: bus.queue(queue_name(view.slug, lane), policy) for lane in lanes}
    queued: set[str] = set()
    for lane, queue in queues.items():
        kept = _sweep(queue, view)
        ids = {event.id for event in kept}
        fresh = [event for event in events if event.data["lane"] == lane and event.id not in ids]
        if moving := behind(kept, fresh, view, now_ms) & offered:
            queue.withdraw(lambda event: event.id in moving)
        queued |= ids - moving
    placed = []
    for event in events:
        if event.id not in queued and queues[event.data["lane"]].offer(event):
            placed.append(event.id)
    return placed


def _sweep(queue: WorkQueue, view: Snapshot) -> list[Envelope]:
    """The queue's waiting offers in stream order once its stale offers are withdrawn."""
    kept: list[Envelope] = []

    def swept(event: Envelope) -> bool:
        if stale(event, view) or event.data.get("quota") != offer_quota.restrictions(view.rows[event.key]):
            return True
        kept.append(event)
        return False

    queue.withdraw(swept)
    return kept


def behind(kept: list[Envelope], fresh: list[Envelope], view: Snapshot, now_ms: int) -> set[str]:
    """The ids of one lane's waiting offers that sit ahead of an offer the hub would offer before them, waiting or
    about to be offered, and are at least one dedupe window old, so offering one again behind it is never dropped by
    the bus dedupe and moves it at most once per window. An offer is graded by the key it is offered by wherever that
    key is the whole order: one under an operator order by its order time then its rank, one of the rest by its rank.
    The urgent or focused offers share one grade, so none of them moves behind another."""
    band = view.classes.band

    def grade(event: Envelope) -> tuple[int, ...]:
        found = band(view.rows[event.key])
        return found[:1] if found[0] == PREFERRED else found

    window = BusConfig().dedupe_window_ms
    best = min(map(grade, fresh), default=LAST)
    moving: set[str] = set()
    for event in reversed(kept):
        own = grade(event)
        if own > best and now_ms - event.time >= window:
            moving.add(event.id)
        best = min(best, own)
    return moving


def stale(event: Envelope, view: Snapshot) -> bool:
    """An offer ``withdrawal`` names against the snapshot, or one whose pin differs from its task's, as when the task
    turned workstation and only the root hive may take it."""
    return bool(withdrawal(event, view) or event.id != offer_id(view.slug, view.rows[event.key]))


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


def take(
    queue: WorkQueue,
    view: Snapshot | Callable[[], Snapshot | None],
    timeout_ms: int,
    fit: Callable[[Envelope], bool] = anywhere,
) -> Delivery | None:
    """A view that yields no snapshot leaves the recheck to the hub, which admits the offer; an offer this hive may
    not take goes back to the queue for another hive without spending one of its deliveries."""
    while (delivery := queue.take(timeout_ms)) is not None:
        if not fit(delivery.event):
            queue.release(delivery, UNFIT_NAK_MS)
            continue
        current = view() if callable(view) else view
        if current is None or not withdrawal(delivery.event, current):
            return delivery
        delivery.term()
    return None
