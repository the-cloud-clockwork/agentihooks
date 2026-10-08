"""Doctor detectors over the agent inbox: items with their histories in, findings out."""

from dataclasses import dataclass

from scripts.doctor.words import gist, plural
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


def findings(items, now_ms, window_ms, receivers=None):
    return [*past_window(items, now_ms, window_ms, receivers), *escalated(items), *no_outcome(items)]


def _waiting(item, receiver, window_ms):
    if not receiver.scoped:
        return False
    return item["state"] == "pending" or not receiver.live or receiver.quiet_ms >= window_ms


def _route(item):
    return f"from {item['sender']} to {item['address']}"


def _steps(item, event):
    return [e for e in item["history"] if e.get("event") == event]


def reader(item):
    """The agent that took delivery of an item, '' while none has."""
    return next((e.get("by", "") for e in reversed(item["history"]) if e.get("state") in ("delivered", "read")), "")


def past_window(items, now_ms, window_ms, receivers=None):
    found, receivers = [], receivers or {}
    for item in items:
        waited = now_ms - item["created_at"]
        if item["state"] in CLOSED or waited < window_ms:
            continue
        receiver = receivers.get(reader(item)) or receivers.get(item["address"], Receiver())
        if not _waiting(item, receiver, window_ms):
            continue
        minutes = waited // MINUTE_MS
        category = "unread delivery" if item["state"] == "pending" else "delivered backlog"
        found.append(
            Finding(
                "inbox past window",
                item["id"],
                f"{category}; sent {plural(minutes, 'minute')} ago",
                (
                    _route(item),
                    gist(item["text"]),
                    plural(len(_steps(item, WOKEN)), "wake"),
                    plural(sum(len(_steps(item, e)) for e in RAISED), "escalation"),
                ),
                f"open past the {plural(window_ms // 1000, 'second')} retry window",
                minutes,
            )
        )
    return found


def escalated(items):
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
                (_route(item), gist(item["text"]), *(f"{RAISED[e['event']]}: {e['reason']}" for e in steps)),
                "an item nobody read after every wake",
                len(steps),
            )
        )
    return found


def no_outcome(items):
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
                (_route(item), gist(item["text"]), f"closed {item['state']} with no outcome named"),
                "a close names where the work went",
                1,
            )
        )
    return found
