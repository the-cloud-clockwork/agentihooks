"""An operator set run window: each tick records elapsed and remaining, alerts the master before the cutoff, and at the
cutoff logs or enforces the window gate and publishes the final state to the ledger and the watchdog. Only swarm set
writes the window, so the tick never extends it."""

import json
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime

from scripts.gates import Who, modes
from scripts.gates import log as gate_log
from scripts.inbox.seats import seat_address
from scripts.inbox.store import InboxStore
from scripts.swarm import capacity, doc_report
from scripts.swarm.store import MASTER, SwarmError

MINUTE_MS = 60_000
ALERT_MINUTES = 30
KEY, SENDER = "run-window", "swarm"
START_KEY, MINUTES_KEY = "window-start", "window-minutes"
KEYS = (START_KEY, MINUTES_KEY)
HELD = ("claimed", "pr")
UNSET = "unset"
SPOKEN = "%d %B %H:%M UTC"
BAD_START = "window-start takes now, off or a UTC time such as 2026-10-25T00:00:00Z"
BAD_MINUTES = "window-minutes takes a whole number of minutes above 0"
EFFECTS = {
    "enforce": "stops new claims and asks every agent in flight to hand off",
    "observe": "logs the claims it would stop and the hand offs it would ask",
    "off": "is skipped",
}
NEXT = {
    "enforce": "the agents in flight hand off and no new task starts until swarm set changes the window.",
    "observe": "the gate only logged, so claims continue; set window-gate=deny to enforce the next cutoff.",
    "off": "the gate is off, so claims continue.",
}
ALERT = (
    "The run window on swarm {slug} ends at {end}, in {minutes} minutes. At the cutoff the window gate, set to {mode}, "
    "{effect}, and the swarm publishes the final state to the ledger and the watchdog. Only swarm set changes the "
    "window. Close this item once you have looked."
)
CUTOFF = "The run window ended at {end}: new claims stop and {count} agents in flight hand off: {names}."
ASK = (
    "The run window on swarm {slug} ended at {end}. Finish the step you are on, push any commits with agentihooks "
    "swarm {slug} publish, write a Handoff v2 for task {task}, run agentihooks swarm {slug} handoff <doc> and stop. "
    "No new task starts until swarm set changes the window."
)
STOPPED = "the run window ended at {end}, no new claims until swarm set changes the window"
FINAL = (
    "Final handoff for the run window on swarm {slug}, {start} to {end}, window gate {mode}. Tasks by state: "
    "{counts}. In flight at the cutoff: {flying}. Next: {next}"
)


@dataclass(frozen=True)
class WindowGate:
    name: str = "window"
    default_mode: str = "observe"


GATE = WindowGate()


def setting(window: dict, key: str, value: str, now_ms: int) -> dict:
    if key == MINUTES_KEY:
        if not value.isdigit() or int(value) == 0:
            raise SwarmError(BAD_MINUTES)
        return {**window, "minutes": int(value)}
    if value == "off":
        return {}
    if value == "now":
        return {**window, "start_ms": now_ms}
    try:
        start = datetime.strptime(value, doc_report.STAMP).replace(tzinfo=UTC)
    except ValueError:
        raise SwarmError(BAD_START) from None
    return {**window, "start_ms": int(start.timestamp() * 1000)}


def reading(window: dict, key: str) -> str:
    if key == MINUTES_KEY:
        return str(window["minutes"]) if "minutes" in window else UNSET
    return doc_report.utc(window["start_ms"]) if "start_ms" in window else UNSET


def span(window: dict) -> tuple[int, int] | None:
    if "start_ms" not in window or "minutes" not in window:
        return None
    return window["start_ms"], window["start_ms"] + window["minutes"] * MINUTE_MS


def report(window: dict, now_ms: int) -> dict:
    bounds = span(window)
    if bounds is None:
        return {}
    start, end = bounds
    return {
        "start": doc_report.utc(start),
        "end": doc_report.utc(end),
        "elapsed_minutes": max(0, now_ms - start) // MINUTE_MS,
        "remaining_minutes": max(0, end - max(now_ms, start)) // MINUTE_MS,
    }


def claims_stopped(config, now_ms: int) -> str:
    bounds = span(config.window)
    if bounds is None or now_ms < bounds[1] or modes.configured(GATE, config.gates) != "enforce":
        return ""
    return STOPPED.format(end=_spoken(bounds[1]))


def step(slug, config, store, ledger, doc, now_ms) -> list[str]:
    bounds = span(config.window)
    if bounds is None or now_ms < bounds[0]:
        return []
    key, window = store.key(slug, KEY), json.dumps(bounds)
    marks = store.redis.hgetall(key)
    if marks.get("window") != window:
        marks = {"window": window}
    progress = report(config.window, now_ms)
    marks.update(elapsed_minutes=progress["elapsed_minutes"], remaining_minutes=progress["remaining_minutes"])
    actions = []
    if bounds[1] - now_ms <= ALERT_MINUTES * MINUTE_MS and "alerted" not in marks:
        marks["alerted"] = now_ms
        actions.append(_alert(slug, config, store, bounds, progress["remaining_minutes"]))
    if now_ms >= bounds[1] and "cutoff" not in marks:
        marks["cutoff"] = now_ms
        actions += _cutoff(slug, config, store, ledger, doc, bounds, now_ms)
    pipe = store.redis.pipeline()
    pipe.delete(key)
    pipe.hset(key, mapping=marks)
    pipe.execute()
    return actions


def in_flight(store, slug, rows: dict) -> list:
    return [
        agent
        for agent in store.agents(slug)
        if agent.lane in capacity.LANES
        and agent.state != "finished"
        and (rows.get(agent.task) or {}).get("state") in HELD
        and rows[agent.task].get("claimed_by") == agent.name
    ]


def final(slug, mode, bounds, rows, flying) -> str:
    counts = Counter(row.get("state") for row in rows.values())
    return FINAL.format(
        slug=slug,
        start=_spoken(bounds[0]),
        end=_spoken(bounds[1]),
        mode=modes.label(mode),
        counts=", ".join(f"{count} {state}" for state, count in sorted(counts.items())) or "none",
        flying="; ".join(f"{a.name} on {a.task} {rows[a.task].get('title', '')}".strip() for a in flying) or "none",
        next=NEXT[mode],
    )


def _spoken(at_ms: int) -> str:
    return datetime.fromtimestamp(at_ms / 1000, UTC).strftime(SPOKEN)


def _alert(slug, config, store, bounds, minutes):
    mode, master = modes.configured(GATE, config.gates), seat_address(slug, MASTER)
    end = _spoken(bounds[1])
    text = ALERT.format(slug=slug, end=end, minutes=minutes, mode=modes.label(mode), effect=EFFECTS[mode])
    InboxStore(store.redis).send(SENDER, master, text, ref=f"{slug}:{KEY}:alert:{bounds[0]}")
    return f"run window: told {master} it ends at {end}"


def _cutoff(slug, config, store, ledger, doc, bounds, now_ms):
    mode, end = modes.configured(GATE, config.gates), _spoken(bounds[1])
    rows = {row["id"]: row for row in doc.get("tasks") or []}
    flying, inbox, actions = in_flight(store, slug, rows), InboxStore(store.redis), []
    if mode != "off":
        kind = "deny" if mode == "enforce" else "observe"
        names = ", ".join(agent.name for agent in flying) or "none"
        reason = CUTOFF.format(end=end, count=len(flying), names=names)
        gate_log.append(slug, gate_log.Row.of(GATE.name, kind, Who(name=SENDER), reason=reason, now_ms=now_ms))
        actions.append(f"run window: {kind} at the cutoff: {reason}")
    if mode == "enforce":
        for agent in flying:
            text = ASK.format(slug=slug, end=end, task=agent.task)
            inbox.send(SENDER, agent.name, text, ref=f"{slug}:{KEY}:{agent.task}", task=agent.task)
        actions.append(f"run window: asked {len(flying)} agents in flight to hand off")
    text = final(slug, mode, bounds, rows, flying)
    ledger.notify(slug, text.replace("-", " ").replace("@", " "))
    inbox.send(SENDER, doc_report.address(), text, ref=f"{slug}:{KEY}:final:{bounds[0]}")
    actions.append("run window: published the final state to the ledger and the watchdog")
    return actions
