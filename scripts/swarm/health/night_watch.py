"""Night watch: each tick, with or without a master, holds the front door seat, the spawn path and the dev merge
queue against a limit. Every action passes the night watch gate, log only by default; its findings show in every
mode."""

import json
import os
from collections.abc import Callable
from dataclasses import dataclass

from scripts.gates import modes
from scripts.gates.base import Who
from scripts.gates.enforcement import NIGHT_WATCH
from scripts.swarm import capacity, operator_email, tick_master, timing
from scripts.swarm.health import merge_stall, spawn_stall
from scripts.swarm.health.findings import MINUTE_MS, Finding
from scripts.swarm.store import MASTER
from scripts.swarm_v2 import front_door

KEY = "night-watch"
SENDER = "swarm"
SEAT_MS, SPAWN_MS, GAP_MS = 10 * MINUTE_MS, 30 * MINUTE_MS, 5 * MINUTE_MS
RESPAWNS = 3
SEAT, SPAWN, MERGE = "seat", "spawn", "merge"
SOURCES = (SEAT, SPAWN, MERGE)
HOST, QUOTA, CAP, SLEEP, UNNAMED = "host room", "quota", "cap", "sleep", "no named hold"
SLEEPING = "the master retired with no new tasks, so the tick spawns nothing until a task is added"
SEAT_ALERT = "The front door master seat of swarm {slug} has been empty for {minutes} minutes: {why}"
SPAWN_ALERT = (
    "Swarm {slug} has had {waiting} ready tasks and no agent launch for {minutes} minutes, held by {hold}: {why}"
)
MERGE_ALERT = "Swarm {slug} dev merges are stuck: {summary}. {evidence}"
CLEARED = "The night watch saw it clear: {what}"
LOGGED = "night watch gate not at deny: would {action}"


@dataclass(frozen=True)
class Watch:
    store: object
    slug: str
    ledger: object
    runtime: object
    doc: dict
    now_ms: int
    sleeping: bool
    respawn: Callable[[], list[str]]
    environ: dict | None = None
    mail: Callable = operator_email.send


def read(store, slug: str) -> dict:
    raw = store.redis.get(store.key(slug, KEY))
    return json.loads(raw) if raw else {}


def run(watch: Watch) -> list[str]:
    state, actions = read(watch.store, watch.slug), []
    try:
        if watch.now_ms - state.get("at", watch.now_ms) > GAP_MS:
            for source in SOURCES:
                actions += _clear(watch, state, source, "the night watch did not run for a while and starts over")
        state["at"] = watch.now_ms
        for step in (_seat, _spawn, _merge):
            actions += step(watch, state)
    finally:
        watch.store.redis.set(watch.store.key(watch.slug, KEY), json.dumps(state))
    return actions


def _allows(watch, action, reason):
    return NIGHT_WATCH.allows(watch.store, Who(name=SENDER, swarm=watch.slug), action, reason, watch.now_ms)


def _minutes(watch, since):
    return (watch.now_ms - since) // MINUTE_MS


def _alert(watch, episode, source, text):
    enforced = modes.configured(NIGHT_WATCH, watch.store.config(watch.slug).gates) == "enforce"
    if episode.get("raised") or (episode.get("logged") and not enforced):
        return []
    if not _allows(watch, f"alert {source}", text):
        episode["logged"] = watch.now_ms
        return [LOGGED.format(action=f"raise the {source} alert: {text}")]
    episode["raised"] = watch.now_ms
    watch.ledger.raise_alert(watch.slug, source, text)
    return [f"raised the {source} alert: {text}"]


def _clear(watch, state, source, what):
    episode = state.pop(source, None)
    return _close(watch, source, what) if (episode or {}).get("raised") else []


def _close(watch, source, what):
    closed = []
    for alert in watch.doc.get("alerts") or []:
        if alert.get("source") == source and alert.get("state") != "done":
            watch.ledger.close_alert(watch.slug, alert["id"], CLEARED.format(what=what))
            closed.append(f"closed the {source} alert: {what}")
    return closed


def _seat_held(watch):
    live = watch.runtime.live_names()
    return any(
        a.lane == MASTER and a.state == "working" and a.name in live and front_door.holds(watch.slug, a.seat)
        for a in watch.store.agents(watch.slug)
    )


def _seat(watch, state):
    if watch.store.config(watch.slug).state != "running" or watch.sleeping or _seat_held(watch):
        return _clear(watch, state, SEAT, "the front door master seat is held again")
    episode = state.setdefault(SEAT, {"since": watch.now_ms})
    minutes = _minutes(watch, episode["since"])
    if watch.now_ms - episode["since"] < SEAT_MS:
        return []
    from scripts.swarm.tick import spawn_holds

    why = "; ".join(spawn_holds(watch.store, watch.slug)) or tick_master._launch_refusal(watch.store, watch.slug)
    episode.update(minutes=minutes, why=why)
    actions = []
    due = watch.now_ms - episode.get("respawned", episode["since"]) >= SEAT_MS
    if due and episode.get("respawns", 0) < RESPAWNS:
        reason = f"front door master seat empty {minutes} minutes: {why}"
        episode["respawned"] = watch.now_ms
        if _allows(watch, "respawn", reason):
            episode["respawns"] = episode.get("respawns", 0) + 1
            actions += watch.respawn() or [f"front door respawn refused: {why}"]
        else:
            actions.append(LOGGED.format(action=f"respawn the front door master, {reason}"))
    return actions + _alert(watch, episode, SEAT, SEAT_ALERT.format(slug=watch.slug, minutes=minutes, why=why))


def held(watch, ready) -> tuple[str, str]:
    from scripts.swarm.tick import _host_full, spawn_holds

    if watch.sleeping:
        return SLEEP, SLEEPING
    if host := _host_full(watch.slug, watch.store, watch.now_ms):
        return HOST, host
    decision, config = capacity.read(watch.store, watch.slug), watch.store.config(watch.slug)
    configured = decision.get("configured") or capacity._configured(config)
    agents = [a for a in watch.store.agents(watch.slug) if a.state != "finished"]
    busy = {lane: sum(a.lane == lane for a in agents) for lane in ready}
    waiting = [lane for lane, tasks in ready.items() if tasks]
    if all(busy[lane] >= configured.get(lane, 0) for lane in waiting):
        return CAP, ", ".join(f"{lane} {busy[lane]} of {configured.get(lane, 0)} seats busy" for lane in waiting)
    if decision and not any((decision.get("placements") or {}).get(lane) for lane in waiting):
        return QUOTA, decision.get("reason") or "no lane has a quota placement"
    return UNNAMED, "; ".join(spawn_holds(watch.store, watch.slug)) or decision.get("reason", "no quota decision")


def _spawn(watch, state):
    if watch.store.config(watch.slug).state != "running":
        return _clear(watch, state, SPAWN, "the swarm is not running")
    _, ready = capacity.ready_work(watch.slug, watch.store, watch.doc)
    waiting = sum(len(tasks) for tasks in ready.values())
    if not waiting:
        return _clear(watch, state, SPAWN, "no ready work waits")
    last, episode = spawn_stall.launched(watch.store, watch.slug), state.get(SPAWN)
    if episode is None or last > episode["launch"]:
        actions = _clear(watch, state, SPAWN, "an agent launched") if episode else []
        state[SPAWN] = {"since": watch.now_ms, "launch": last}
        return actions
    if watch.now_ms - episode["since"] < SPAWN_MS:
        return []
    hold, why = held(watch, ready)
    minutes = _minutes(watch, episode["since"])
    episode.update(hold=hold, why=why, waiting=waiting, minutes=minutes)
    text = SPAWN_ALERT.format(slug=watch.slug, waiting=waiting, minutes=minutes, hold=hold, why=why)
    return _alert(watch, episode, SPAWN, text)


def _merge(watch, state):
    config = watch.store.config(watch.slug)
    if config.state != "running" or watch.sleeping:
        return _clear(watch, state, MERGE, "the swarm is not running")
    found = timing.call(merge_stall.findings, watch.store, watch.slug, config.repo, watch.now_ms)
    if not found:
        return _clear(watch, state, MERGE, "dev merges again")
    observed = json.loads(watch.store.redis.get(watch.store.key(watch.slug, "merge-stall")) or "{}")
    kinds, episode, actions = sorted(finding.kind for finding in found), state.get(MERGE), []
    if episode is None or episode["merged"] != observed.get("merged") or not set(kinds) <= set(episode["kinds"]):
        if (episode or {}).get("raised"):
            actions += _close(watch, MERGE, "a new merge finding replaces it")
        episode = state[MERGE] = {"merged": observed.get("merged"), "kinds": kinds}
    summary = "; ".join(finding.summary for finding in found)
    evidence = "; ".join(line for finding in found for line in finding.evidence)
    text = MERGE_ALERT.format(slug=watch.slug, summary=summary, evidence=evidence)
    raised = _alert(watch, episode, MERGE, text)
    if raised and episode.get("raised") == watch.now_ms:
        env = os.environ if watch.environ is None else watch.environ
        refused = watch.mail(f"Swarm {watch.slug}: dev merges are stuck", text, env, config.repo)
        raised.append(f"email to the operator refused: {refused}" if refused else f"emailed the operator: {summary}")
    return actions + raised


def findings(store, slug: str) -> list[Finding]:
    state, found = read(store, slug), []
    if seat := state.get(SEAT, {}).get("why"):
        found.append(
            Finding(
                "empty master seat",
                slug,
                f"The front door master seat has been empty for {state[SEAT]['minutes']} minutes",
                (f"Why it stays empty: {seat}",),
                "ten minutes without a live front door master",
                state[SEAT]["minutes"],
            )
        )
    if hold := state.get(SPAWN, {}).get("hold"):
        episode = state[SPAWN]
        found.append(
            Finding(
                "held spawn",
                slug,
                f"{episode['waiting']} ready tasks and no agent launch for {episode['minutes']} minutes, held by {hold}",
                (f"Hold: {hold}", f"Evidence: {episode['why']}"),
                "thirty minutes of ready work without a launch",
                episode["minutes"],
            )
        )
    return found
