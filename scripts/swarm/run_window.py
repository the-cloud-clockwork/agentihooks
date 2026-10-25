"""An operator set run window: each tick records elapsed and remaining, alerts the master before the cutoff, and at the
cutoff logs or enforces the window gate and publishes the final state to the ledger and the watchdog. Only swarm set
writes the window, so the tick never extends it."""

import hashlib
import json
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime

from scripts.gates import Who, modes
from scripts.gates import log as gate_log
from scripts.inbox.seats import seat_address
from scripts.inbox.store import DuplicateItem, InboxStore
from scripts.swarm import capacity, doc_report
from scripts.swarm.store import MASTER, SwarmError
from scripts.swarm_ledger import ledger_notifications

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
    "The run window on swarm {slug} ends at {end}, in {minutes}. At the cutoff the window gate, set to {mode}, "
    "{effect}, and the swarm publishes the final state to the ledger and the watchdog. Only swarm set changes the "
    "window. Close this item once you have looked."
)
CUTOFF = "The run window {start} to {end} ended: new claims stop and {agents} in flight hand off: {names}."
ASK = (
    "The run window on swarm {slug} ended at {end}. Finish the step you are on, push any commits with agentihooks "
    "swarm {slug} publish, write a Handoff v2 for task {task}, run agentihooks swarm {slug} handoff <doc> and stop. "
    "No new task starts until swarm set changes the window."
)
STOPPED = "the run window ended at {end}, no new claims until swarm set changes the window"
SHORT = {
    "enforce": "Agents in flight hand off and claims stay stopped.",
    "observe": "Claims continue; window-gate=deny enforces the next cutoff.",
    "off": "Claims continue.",
}
NOTICE = "Run window ended {end}, gate {mode}. Full handoff: {watchdog}. Tasks: {counts}. In flight: {flying}. {next}"
FINAL = (
    "## Intent\nRun window on swarm {slug}, {start} to {end}, window gate {mode}.\n\n"
    "## Done\nTasks by state: {counts}.\n\n"
    "## Stopped at\nIn flight at the cutoff: {flying}.\n\n"
    "## Decisions and promises\nOnly swarm set changes the window.\n\n"
    "## Next\n{next}\n\n"
    "## Read first\n{read}\n\n"
    "<!-- handoff complete -->"
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


def claims_stopped(store, slug, config, now_ms: int) -> str:
    bounds = span(config.window)
    if bounds is None or now_ms < bounds[1]:
        return ""
    marks = store.redis.hgetall(store.key(slug, KEY))
    decided = marks.get("mode") if marks.get("window") == json.dumps(bounds) else None
    if (decided or modes.configured(GATE, config.gates)) != "enforce":
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
    _save(store, key, marks)
    rows, actions = {row["id"]: row for row in doc.get("tasks") or []}, []
    if now_ms < bounds[1] <= now_ms + ALERT_MINUTES * MINUTE_MS and "alerted" not in marks:
        actions.append(_alert(slug, config, store, bounds, progress["remaining_minutes"]))
        _save(store, key, marks, alerted=now_ms)
    if now_ms >= bounds[1] and "cutoff" not in marks:
        mode = modes.configured(GATE, config.gates)
        actions += _gate(slug, mode, store, rows, bounds, now_ms)
        _save(store, key, marks, cutoff=now_ms, mode=mode)
    if now_ms >= bounds[1] and "published" not in marks:
        actions.append(_publish(slug, marks["mode"], store, ledger, rows, bounds))
        _save(store, key, marks, published=now_ms)
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
    return FINAL.format(
        slug=slug,
        start=_spoken(bounds[0]),
        end=_spoken(bounds[1]),
        mode=modes.label(mode),
        counts=_counts(rows),
        flying="; ".join(f"{a.name} on {a.task} {rows[a.task].get('title', '')}".strip() for a in flying) or "none",
        next=NEXT[mode],
        read="\n".join(f"- ledger:{slug}/tasks/{a.task}" for a in flying) or "None",
    )


def notice(mode, bounds, rows, flying) -> str:
    fields = {
        "end": _spoken(bounds[1]),
        "mode": modes.label(mode),
        "counts": _counts(rows),
        "next": SHORT[mode],
        "watchdog": doc_report.address(),
    }
    text = NOTICE.format(flying=", ".join(f"{a.name} on {a.task}" for a in flying) or "none", **fields)
    if len(text) <= ledger_notifications.TEXT_KEPT:
        return text
    return NOTICE.format(flying=_count(len(flying), "agent"), **fields)


def _counts(rows) -> str:
    counts = Counter(row.get("state") for row in rows.values())
    return ", ".join(f"{count} {state}" for state, count in sorted(counts.items())) or "none"


def _spoken(at_ms: int) -> str:
    return datetime.fromtimestamp(at_ms / 1000, UTC).strftime(SPOKEN)


def _alert(slug, config, store, bounds, minutes):
    mode, master = modes.configured(GATE, config.gates), seat_address(slug, MASTER)
    end = _spoken(bounds[1])
    text = ALERT.format(
        slug=slug, end=end, minutes=_count(minutes, "minute"), mode=modes.label(mode), effect=EFFECTS[mode]
    )
    _send_once(store, master, text, f"{slug}:{KEY}:alert:{_ref(bounds)}")
    return f"run window: told {master} it ends at {end}"


def _gate(slug, mode, store, rows, bounds, now_ms):
    end, flying, actions = _spoken(bounds[1]), in_flight(store, slug, rows), []
    if mode == "enforce":
        for agent in flying:
            text, ref = ASK.format(slug=slug, end=end, task=agent.task), f"{slug}:{KEY}:{_ref(bounds)}:{agent.task}"
            _send_once(store, agent.name, text, ref, agent.task)
    if mode != "off":
        kind = "deny" if mode == "enforce" else "observe"
        names = ", ".join(agent.name for agent in flying) or "none"
        start = _spoken(bounds[0])
        reason = CUTOFF.format(start=start, end=end, agents=_count(len(flying), "agent"), names=names)
        opening = CUTOFF.split("{agents}")[0].format(start=start, end=end)
        logged = any(
            row.get("gate") == GATE.name and str(row.get("reason")).startswith(opening)
            for row in gate_log.since(slug, bounds[1])
        )
        if not logged:
            gate_log.append(slug, gate_log.Row.of(GATE.name, kind, Who(name=SENDER), reason=reason, now_ms=now_ms))
        actions.append(f"run window: {kind} at the cutoff: {reason}")
    if mode == "enforce":
        actions.append(f"run window: asked {_count(len(flying), 'agent')} in flight to hand off")
    return actions


def _publish(slug, mode, store, ledger, rows, bounds):
    flying = in_flight(store, slug, rows)
    _send_once(
        store, doc_report.address(), final(slug, mode, bounds, rows, flying), f"{slug}:{KEY}:final:{_ref(bounds)}"
    )
    ledger.notify(slug, notice(mode, bounds, rows, flying))
    return "run window: published the final state to the ledger and the watchdog"


def _send_once(store, address, text, ref, task=""):
    try:
        InboxStore(store.redis).send(SENDER, address, text, ref=ref, task=task, item_id=_item_id(ref))
    except DuplicateItem:
        pass


def _item_id(ref: str) -> str:
    return hashlib.sha256(ref.encode()).hexdigest()[:12]


def _ref(bounds) -> str:
    return f"{bounds[0]}-{bounds[1]}"


def _count(count: int, noun: str) -> str:
    return f"{count} {noun}" if count == 1 else f"{count} {noun}s"


def _save(store, key, marks, **stamp):
    marks.update(stamp)
    pipe = store.redis.pipeline()
    pipe.delete(key)
    pipe.hset(key, mapping=marks)
    pipe.execute()
