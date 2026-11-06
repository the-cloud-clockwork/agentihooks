"""Degradation held for an hour asks the front door master: is this a panic mode call.

Code measures each signal's start: no agent launch while a lane could take one, no merge into dev while the merge
queue holds entries, a tick step failure category still recurring, tick passes ending early on one missing value, and
an urgent task held open. At the first hour a signal holds and every hour after, the panic-call gate acts by its mode
in the swarm config: observe logs the question to the swarm gate log, enforce sends it to the master seat's inbox,
off skips.
"""

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from scripts.gates import Who, modes
from scripts.gates import log as gate_log
from scripts.inbox.seats import seat_address
from scripts.inbox.store import DuplicateItem, InboxStore
from scripts.swarm import blockers, timing
from scripts.swarm.health import early_ends, spawn_stall, urgent_hold

HOUR_MS = 3_600_000
MINUTE_MS = 60_000
KEY = "panic-call"
MERGE_KEY = "merge-stall"
SPAWN_STALL = "spawn stall"
SENDER = "swarm"
MASTER = "master"
SPOKEN = "%d %B %H:%M UTC"
LAUNCH = "a lane has a free seat, quota and claimable work and no agent launched"
UNCLAIMED = "it is claimable but remains unclaimed"
QUESTION = (
    "Is this a panic mode call? {name} on swarm {slug} for {minutes} minutes, since {start}. Evidence: {evidence}. "
    "Yes: fix it yourself now; agentihooks swarm {slug} panic on --fix <pr url> raises panic and opens the master "
    "coding window, and agentihooks swarm {slug} merge priority <pr url> puts the fix first in the queue, even if "
    "that stops every other merge. No: say why on the ledger with agentihooks ledger --slug {slug} --as <your name> "
    'say "<reason>", then close this item.'
)


@dataclass(frozen=True)
class PanicCallGate:
    name: str = KEY
    default_mode: str = "observe"


GATE = PanicCallGate()


@dataclass(frozen=True)
class Signal:
    name: str
    since: int
    evidence: str


def read(store: Any, slug: str) -> dict[str, Any]:
    """Return the hours already asked per signal and the early end streak start."""
    raw = store.redis.get(store.key(slug, KEY))
    return json.loads(raw) if raw else {}


def _launch(store: Any, slug: str) -> list[Signal]:
    stall = spawn_stall.read(store, slug)
    return [Signal("No agent launch", stall["since"], LAUNCH)] if "since" in stall else []


def _merge(store: Any, slug: str) -> list[Signal]:
    state = json.loads(store.redis.get(store.key(slug, MERGE_KEY)) or "{}")
    if not state.get("queue") or not state.get("merged") or state.get("error"):
        return []
    return [Signal("No merge into dev", state["merged"], f"{state['queue']} queued entries on {state['repo']}")]


def _steps(store: Any, slug: str, at: int) -> list[Signal]:
    return [
        Signal(
            f"Tick step failing with {category}",
            found.first_seen,
            f"{found.count} failures, latest: {found.detail}",
        )
        for category, found in sorted(blockers.active(store, slug, at).items())
        if category != SPAWN_STALL and at - found.last_seen <= blockers.RECENT_MS
    ]


def _early(store: Any, slug: str, at: int, previous: dict[str, Any]) -> tuple[list[Signal], dict[str, Any]]:
    streak = early_ends.read(store, slug)
    if not streak.get("count"):
        return [], {}
    value = streak["value"]
    kept = previous if previous.get("value") == value else {"value": value, "since": at}
    evidence = f"{streak['count']} passes in a row ended early because no step stored {value}"
    return [Signal("Tick passes ending early", kept["since"], evidence)], kept


def _urgent(store: Any, slug: str) -> list[Signal]:
    held = urgent_hold.read(store, slug).get("tasks", {})
    return [
        Signal(f"Urgent task {row['title']} held", row["since"], row["why"] or UNCLAIMED)
        for _, row in sorted(held.items())
    ]


def signals(store: Any, slug: str, at: int, early: dict[str, Any]) -> tuple[list[Signal], dict[str, Any]]:
    """Return every degradation signal holding now with its measured start, and the early end streak start to keep."""
    ended, kept = _early(store, slug, at, early)
    found = [*_launch(store, slug), *_merge(store, slug), *_steps(store, slug, at), *ended, *_urgent(store, slug)]
    return found, kept


def _spoken(at_ms: int) -> str:
    return datetime.fromtimestamp(at_ms / 1000, UTC).strftime(SPOKEN)


def question(slug: str, signal: Signal, at: int) -> str:
    """Return the question naming the signal, its evidence, its duration and both answers."""
    minutes = (at - signal.since) // MINUTE_MS
    return QUESTION.format(
        name=signal.name, slug=slug, minutes=minutes, start=_spoken(signal.since), evidence=signal.evidence
    )


def _ask(store: Any, slug: str, mode: str, text: str, ref: str, at: int) -> str:
    if mode != "enforce":
        gate_log.append(slug, gate_log.Row.of(GATE.name, "observe", Who(name=SENDER), reason=text, now_ms=at))
        return f"panic call: logged {text}"
    master = seat_address(slug, MASTER)
    try:
        InboxStore(store.redis).send(
            SENDER, master, text, ref=ref, item_id=hashlib.sha256(ref.encode()).hexdigest()[:12]
        )
    except DuplicateItem:
        pass
    return f"panic call: asked {master}: {text}"


def step(store: Any, slug: str, at: int) -> list[str]:
    """Ask once for each signal at its first held hour and once per hour after, by the panic-call gate mode."""
    mode = modes.configured(GATE, store.config(slug).gates)
    if mode == "off":
        return []
    previous = read(store, slug)
    found, early = signals(store, slug, at, previous.get("early", {}))
    before, asked, lines = previous.get("asked", {}), {}, []
    for signal in found:
        key, hours = f"{signal.name}|{signal.since}", (at - signal.since) // HOUR_MS
        asked[key] = before.get(key, 0)
        if hours < 1 or hours <= asked[key]:
            continue
        asked[key] = hours
        lines.append(_ask(store, slug, mode, question(slug, signal, at), f"{KEY}:{slug}:{key}:{hours}", at))
    timing.keep()
    store.redis.set(store.key(slug, KEY), json.dumps({"asked": asked, "early": early}))
    return lines
