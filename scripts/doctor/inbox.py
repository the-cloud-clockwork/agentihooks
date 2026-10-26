"""Doctor detectors over the agent inbox: items with their histories in, findings out."""

from dataclasses import dataclass

from scripts.doctor.words import gist, plural
from scripts.inbox.seats import is_seat
from scripts.inbox.store import CLOSED
from scripts.inbox.wake import TO_MASTER, TO_OPERATOR, WOKEN
from scripts.swarm.health.findings import MINUTE_MS, Finding

RAISED = {TO_MASTER: "raised to the master", TO_OPERATOR: "raised to the operator"}
BARE = ("done", "cancelled")


@dataclass(frozen=True)
class Receiver:
    """Who an address reaches: scoped is False for a session outside the swarm, quiet_ms how long a live one sat idle."""

    scoped: bool = True
    live: bool = False
    quiet_ms: int = 0
    occupant: str = ""
    harness: str = ""
    seat_wait: bool = False
    task: str = ""
    bound_at: int = 0


def findings(items, now_ms, window_ms, receivers=None):
    return [
        *past_window(items, now_ms, window_ms, receivers),
        *escalated(items, receivers),
        *no_outcome(items, receivers),
    ]


def _waiting(item, receiver, window_ms):
    if not receiver.scoped:
        return False
    return item["state"] == "pending" or not receiver.live or receiver.quiet_ms >= window_ms


def _route(item):
    return f"from {item['sender']} to {item['address']}"


def _recipient(item, receivers):
    receiver = (receivers or {}).get(item["address"])
    if receiver is None:
        return "recipient state unavailable"
    if receiver.live:
        return f"occupant {receiver.occupant}; harness {receiver.harness or 'unknown'}"
    return "seat is empty" if is_seat(item["address"]) else "recipient has no live occupant"


def _steps(item, event):
    return [e for e in item["history"] if e.get("event") == event]


def reader(item):
    """The agent that took delivery of an item, '' while none has."""
    return next((e.get("by", "") for e in reversed(item["history"]) if e.get("state") in ("delivered", "read")), "")


def _task_mismatch(item, receivers):
    receiver = receivers.get(item["address"], Receiver())
    task = item.get("task", "")
    if (
        item["state"] == "pending"
        and is_seat(item["address"])
        and receiver.live
        and task
        and receiver.task
        and task != receiver.task
    ):
        return (f"notice task {task}; occupant holds {receiver.task}",)
    return ()


def past_window(items, now_ms, window_ms, receivers=None):
    found, receivers = [], receivers or {}
    for item in items:
        waited = now_ms - item["created_at"]
        bound_at = receivers.get(item["address"], Receiver()).bound_at
        if item.get("fyi") or item["state"] in CLOSED or now_ms - max(item["created_at"], bound_at) < window_ms:
            continue
        receiver = receivers.get(reader(item)) or receivers.get(item["address"], Receiver())
        if not _waiting(item, receiver, window_ms):
            continue
        if not receiver.live and receivers.get(item["address"], Receiver()).seat_wait:
            continue
        minutes = waited // MINUTE_MS
        mismatch = _task_mismatch(item, receivers)
        category = (
            "task ownership mismatch"
            if mismatch
            else ("unread delivery" if item["state"] == "pending" else "delivered backlog")
        )
        found.append(
            Finding(
                "inbox past window",
                item["id"],
                f"{category}; sent {plural(minutes, 'minute')} ago",
                (
                    _route(item),
                    gist(item["text"]),
                    _recipient(item, receivers),
                    plural(len(_steps(item, WOKEN)), "wake"),
                    plural(sum(len(_steps(item, e)) for e in RAISED), "escalation"),
                    *mismatch,
                ),
                f"open past the {plural(window_ms // 1000, 'second')} retry window",
                minutes,
                evidence_identity=(
                    item["state"],
                    len(_steps(item, WOKEN)),
                    sum(len(_steps(item, e)) for e in RAISED),
                    *mismatch,
                ),
            )
        )
    return found


def escalated(items, receivers=None):
    found = []
    for item in items:
        steps = [e for e in item["history"] if e.get("event") in RAISED]
        if not steps:
            continue
        found.append(
            Finding(
                "inbox escalation",
                item["id"],
                f"{RAISED[steps[-1]['event']]}, now {item['state']}",
                (
                    _route(item),
                    gist(item["text"]),
                    _recipient(item, receivers),
                    *(f"{RAISED[e['event']]}: {e['reason']}" for e in steps),
                ),
                "an item nobody read after every wake",
                len(steps),
            )
        )
    return found


def no_outcome(items, receivers=None):
    found = []
    for item in items:
        if item.get("fyi") or item["state"] not in BARE or item["reason"].strip() not in ("", item["state"]):
            continue
        closer = next((e["by"] for e in reversed(item["history"]) if e.get("state") == item["state"]), "")
        found.append(
            Finding(
                "inbox no outcome",
                item["id"],
                f"closed {item['state']} by {closer or 'unknown'} without naming where the work went",
                (
                    _route(item),
                    gist(item["text"]),
                    _recipient(item, receivers),
                    f"closed {item['state']} with no outcome named",
                ),
                "a close names where the work went",
                1,
            )
        )
    return found
