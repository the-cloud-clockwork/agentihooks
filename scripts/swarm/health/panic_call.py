"""Degradation held for an hour asks the front door master: is this a panic mode call.

Code measures each signal's start: no agent launch while a lane could take one, no merge into dev while the merge
queue holds entries, a tick step failure category still recurring, tick passes ending early on one missing value, an
urgent task held open or blocked, and an urgent claim quiet past the stale claim window, which is asked at that window
rather than the hour. At the first hour a signal holds and every hour after, the panic-call gate acts by
its mode in the swarm config: observe logs the question to the swarm gate log, enforce sends it to the master seat's
inbox, off skips.
"""

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from scripts.gates import Who, modes
from scripts.gates import log as gate_log
from scripts.gates import quiet as quiet_gate
from scripts.inbox.seats import seat_address  # pyright: ignore[reportUnknownVariableType]
from scripts.inbox.store import DuplicateItem, InboxStore
from scripts.swarm import blockers, timing
from scripts.swarm.health import early_ends, spawn_stall, urgent_hold
from scripts.swarm.health.findings import limits  # pyright: ignore[reportUnknownVariableType]

HOUR_MS = 3_600_000
MINUTE_MS = 60_000
KEY = "panic-call"
LAUNCH_KEY = spawn_stall.KEY
MERGE_KEY = "merge-stall"
URGENT_KEY = urgent_hold.KEY
SPAWN_STALL = "spawn stall"
SENDER = "swarm"
MASTER = "master"
SPOKEN = "%d %B %H:%M UTC"
LAUNCH = "a lane has a free seat, quota and claimable work and no agent launched"
UNCLAIMED = "it is claimable but remains unclaimed"
QUIET = "claimed by {name} with no ledger change for {minutes} minutes"
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
    after: int = HOUR_MS


def _stored(store: Any, slug: str, key: str) -> dict[str, Any]:
    raw = store.redis.get(store.key(slug, key))
    return dict[str, Any](json.loads(raw)) if raw else {}


def read(store: Any, slug: str) -> dict[str, Any]:
    """Return the saved state: ``asked`` hours per signal and the ``early`` streak start."""
    return _stored(store, slug, KEY)


def _launch(store: Any, slug: str) -> list[Signal]:
    stall = _stored(store, slug, LAUNCH_KEY)
    return [Signal("No agent launch", stall["since"], LAUNCH)] if "since" in stall else []


def _merge(store: Any, slug: str) -> list[Signal]:
    state = _stored(store, slug, MERGE_KEY)
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
    held = dict[str, Any](_stored(store, slug, URGENT_KEY).get("tasks", {}))
    return [
        Signal(f"Urgent task {row['title']} held", row["since"], row["why"] or UNCLAIMED)
        for _, row in sorted(held.items())
    ]


def _quiet(store: Any, slug: str, at: int) -> list[Signal]:
    claimed = dict[str, Any](_stored(store, slug, URGENT_KEY).get(urgent_hold.CLAIMED, {}))
    stale = limits().stale_minutes * MINUTE_MS
    found: list[Signal] = []
    for agent in store.agents(slug):
        held = bool(quiet_gate.holding(agent, claimed))  # pyright: ignore[reportUnknownMemberType]
        if not held or quiet_gate.declared_wait(store.redis, slug, agent.name, at):  # pyright: ignore[reportUnknownMemberType]
            continue
        since = quiet_gate.last_signal(store.redis, slug, agent)  # pyright: ignore[reportUnknownMemberType]
        if since and at - since >= stale:
            evidence = QUIET.format(name=agent.name, minutes=(at - since) // MINUTE_MS)
            found.append(Signal(f"Urgent task {claimed[agent.task]['title']} claim quiet", since, evidence, stale))
    return sorted(found, key=lambda signal: signal.name)


def signals(store: Any, slug: str, at: int, early: dict[str, Any]) -> tuple[list[Signal], dict[str, Any]]:
    """Return every degradation signal holding now with its measured start, and the early end streak start to keep."""
    ended, kept = _early(store, slug, at, early)
    found = [*_launch(store, slug), *_merge(store, slug), *_steps(store, slug, at), *ended, *_urgent(store, slug)]
    return [*found, *_quiet(store, slug, at)], kept


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
        who = Who(name=SENDER)
        row = gate_log.Row.of(GATE.name, "observe", who, reason=text, now_ms=at)  # pyright: ignore[reportUnknownMemberType]
        gate_log.append(slug, row)  # pyright: ignore[reportUnknownMemberType]
        return f"panic call: logged {text}"
    master = str(seat_address(slug, MASTER))
    try:
        InboxStore(store.redis).send(
            SENDER, master, text, ref=ref, item_id=hashlib.sha256(ref.encode()).hexdigest()[:12]
        )
    except DuplicateItem:
        return f"panic call: already asked {master}: {text}"
    return f"panic call: asked {master}: {text}"


def _save(store: Any, slug: str, asked: dict[str, int], early: dict[str, Any]) -> None:
    key = store.key(slug, KEY)
    timing.keep()
    if asked or early:
        store.redis.set(key, json.dumps({"asked": asked, "early": early}))
    else:
        store.redis.delete(key)


def step(store: Any, slug: str, at: int) -> list[str]:
    """Ask once for each signal at its first held hour and once per hour after, by the panic-call gate mode."""
    mode = str(modes.configured(GATE, store.config(slug).gates))  # pyright: ignore[reportUnknownMemberType]
    if mode == "off":
        return []
    previous = read(store, slug)
    found, early = signals(store, slug, at, previous.get("early", {}))
    before = dict[str, int](previous.get("asked", {}))
    asked: dict[str, int] = {}
    lines: list[str] = []
    for signal in found:
        key, hours = f"{signal.name}|{signal.since}", (at - signal.since - signal.after) // HOUR_MS + 1
        if hours < 1 or hours <= before.get(key, 0):
            if key in before:
                asked[key] = before[key]
            continue
        asked[key] = hours
        lines.append(_ask(store, slug, mode, question(slug, signal, at), f"{KEY}:{slug}:{key}:{hours}", at))
    _save(store, slug, asked, early)
    return lines
