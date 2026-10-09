"""The swarm status report the ledger page reads, built from the swarm store and a ledger state already in hand."""

import json
import os
import time
from datetime import date, datetime

from scripts.agents_quota import page_quota
from scripts.gates import catalog, lift, modes, progress
from scripts.gates import log as gate_log
from scripts.gates import quiet as quiet_gate
from scripts.gates.talk import WORKER_LANES
from scripts.handoff import transfers
from scripts.inbox.store import InboxStore
from scripts.swarm import (
    affinity,
    drain_watch,
    idle,
    launch_check,
    live_binding,
    overlays,
    quota_view,
    retire_watch,
    snapshot,
    tick_master,
)
from scripts.swarm.health import activity, checks, verdicts
from scripts.swarm.health import findings as health
from scripts.swarm.naming import swarm_name
from scripts.swarm.store import ASSIST, SwarmError
from scripts.swarm.tick import STARTUP_GRACE_MS, agent_status
from scripts.swarm_ledger import plan_shape
from scripts.swarm_v2.runtime import observe

DEFAULT_COMPACT_LIMIT = 600
UNCLASSIFIED = "unclassified"


def now_ms():
    return int(time.time() * 1000)


def task_counts(tasks):
    return {s: sum(1 for t in tasks if t.get("state") == s) for s in ("open", "claimed", "blocked", "pr", "done")}


def auto_snapshot(config):
    return {
        "last": snapshot.last_auto(config.slug),
        "kept": len(snapshot.automatic(config.slug)),
        "every_minutes": snapshot.interval_minutes(config, os.environ),
    }


def verdict_store(store, slug):
    return verdicts.VerdictStore(store.redis, store.key(slug, "findings"))


def findings(store, slug, config, tasks, events):
    agents, limits = store.agents(slug), health.limits()
    events = [
        *events,
        *(
            {
                "kind": "task started" if row["state"] == "started" else "launch failed",
                "target": f"tasks/{row['task']}",
                "error": row["error"],
            }
            for row in store.launches(slug)
            if row["state"] in ("started", "failed")
        ),
    ]
    quiet = quiet_gate.quiet_minutes(store.redis, slug, agents, {t["id"]: t for t in tasks}, now_ms())
    rows = _health_rows(store, slug, agents, quiet, now_ms())
    return verdict_store(store, slug).visible(
        health.findings(
            {"tasks": tasks, "_meta": {"events": events}},
            rows,
            activity.counts(slug),
            limits,
            checks.waiting(
                rows,
                tasks,
                limits,
                checks.cached(store.redis, store.key(slug, "checks"), approval=config.autonomy == ASSIST),
            ),
            checks.green(tasks, checks.cached_green(store.redis, store.key(slug, "checks"))),
            talk_since_outcome(store, slug, rows),
        )
        + live_binding.findings(store, slug)
        + retire_watch.findings(store, slug)
        + drain_watch.findings(store, slug, limits, now_ms())
        + launch_check.findings(store, slug),
        now_ms(),
        limits.cooldown_minutes * 60_000,
    )


def _health_rows(store, slug, agents, quiet, at):
    latest = activity.last_events(slug)
    rows = []
    for agent in agents:
        started = launch_check.session_started_at(agent)
        last = max(latest.get(agent.name, 0), started)
        minutes = (at - last) // health.MINUTE_MS if last else None
        if at - started <= STARTUP_GRACE_MS or quiet_gate.declared_wait(store.redis, slug, agent.name, at):
            minutes = None
        rows.append({**agent.__dict__, "quiet_minutes": quiet.get(agent.name), "tool_quiet_minutes": minutes})
    return rows


def talk_since_outcome(store, slug, rows):
    marks = progress.Progress(store.redis, slug)
    return {row["name"]: marks.read(row["name"]).talk for row in rows if row.get("lane") in WORKER_LANES}


def compact_limit(config):
    return config.compact_limit or int(os.environ.get("AGENTIHOOKS_COMPACT_LIMIT") or DEFAULT_COMPACT_LIMIT)


def done_today(tasks, events, since_ms):
    done = {t["id"] for t in tasks if t.get("state") == "done"}
    marked = {
        e["target"].removeprefix("tasks/") for e in events if e.get("kind") == "task done" and e["at"] >= since_ms
    }
    return len(done & marked)


def local_midnight_ms():
    return int(datetime.combine(date.today(), datetime.min.time()).timestamp() * 1000)


def doctor_report(store, slug):
    from scripts.doctor import loop

    doctor = store.peer(slug)
    if not doctor or doctor not in store.slugs():
        return {"slug": "", "state": "not running", "last_check": 0, "findings": 0}
    return {
        "slug": doctor,
        "state": store.config(doctor).state,
        "last_check": int(store.redis.hget(loop._timer_key(store, doctor), "last") or 0),
        "findings": store.redis.hlen(loop.verdicts(store, doctor).key),
    }


def handoff_rows(transfers_list, agents):
    latest = {row["seat"]: row for row in transfers_list}
    occupant = {a["seat"]: a for a in agents if a.get("seat")}
    seats = sorted(set(latest) | set(occupant), key=lambda seat: (not seat.startswith("master@"), seat))
    rows = []
    for seat in seats:
        row, agent = latest.get(seat, {}), occupant.get(seat, {})
        awaiting = agent.get("name", "") if agent.get("state") == "awaiting-decision" else ""
        binding = row.get("binding", {}).get("state", "live" if agent else "")
        rows.append(
            {
                "seat": seat,
                "at": row.get("at", 0),
                "reason": row.get("reason", ""),
                "continuity": row.get("continuity", {}).get("state", ""),
                "binding": "awaiting decision" if awaiting else binding,
                "bound_at": row.get("binding", {}).get("at", 0),
                "successor": row.get("successor") or agent.get("name", ""),
                "awaiting": awaiting,
            }
        )
    return rows


def shape_report(tasks: list[dict], max_eng: int) -> dict:
    try:
        return plan_shape.report(tasks, max_eng)
    except SwarmError as exc:
        return {"error": str(exc)}


def observation(store, slug, agent):
    found = observe.stored(store, slug, agent.execution_id) if agent.execution_id else None
    if found:
        return {
            "state": found.state.value,
            "terminal": found.terminal.value,
            "failure": found.failure.value,
            "confidence": found.confidence.value,
            "needs_operator": found.needs_operator,
            "observed_at": found.observed_at,
            "confirmed_at": found.confirmed_at,
            "sources": found.sources,
        }
    beat = idle.heartbeat(store.redis, slug, agent.name)
    if not beat:
        return {"state": UNCLASSIFIED, "observed_at": None, "sources": {}}
    at = beat["at"] / 1000
    heartbeat = {"reading": observe.Reading.OK.value, "observed_at": at, "value": beat["state"]}
    return {"state": UNCLASSIFIED, "observed_at": at, "sources": {observe.Source.HEARTBEAT.value: heartbeat}}


def status_report(store, slug, state):
    from scripts.swarm import capacity

    config = store.config(slug)
    tasks = state.get("tasks", [])
    events = state.get("_meta", {}).get("events", [])
    agents = [a.__dict__ for a in store.agents(slug)]
    handed = transfers.list_transfers(store, slug)
    active = lift.active(gate_log.recent(slug, limit=None), now_ms())
    promotion = tick_master.read(store, slug)
    return {
        "config": {
            **config.__dict__,
            "name": swarm_name(config.code),
        },
        "agents": [
            {
                **a.__dict__,
                "status": agent_status(a),
                "promoted": a.name == promotion.get("promoted"),
                "state_since": int(store.redis.hget(store.key(slug, "state-since"), a.name) or 0),
                "observation": observation(store, slug, a),
                "gates": active.get(a.name, []),
                "inbox": [
                    {"text": item.text, "sender": item.sender, "state": item.state}
                    for item in InboxStore(store.redis).pending_mail(a.name)
                ],
            }
            for a in store.agents(slug)
        ],
        "last_tick": int(store.redis.get(store.key(slug, "last-tick")) or 0),
        "history": [json.loads(row) for row in store.redis.lrange(store.key(slug, "history"), 0, -1)],
        "tasks": task_counts(tasks),
        "spawns": store.spawns(slug),
        "findings": findings(store, slug, config, tasks, events),
        "launch_checks": launch_check.reports(store, slug),
        "auto_snapshot": auto_snapshot(config),
        "restored": store.restored(slug),
        "transfers": handed,
        "handoffs": handoff_rows(handed, agents),
        "peer": store.peer(slug),
        "plan_shape": shape_report(tasks, config.max_eng),
        "compact_limit": compact_limit(config),
        "done_today": done_today(tasks, events, local_midnight_ms()),
        "doctor": doctor_report(store, slug),
        "quota": page_quota(),
        "quota_capacity": quota_view.page(capacity.read(store, slug)),
        "gates": [{**row, "kind": modes.label(row["kind"])} for row in gate_log.decisions(slug)],
        "gate_modes": {name: modes.label(mode) for name, mode in catalog.current(config.gates).items()},
        "master_affinity": affinity.report(store, slug, config, store.agents(slug)),
        "promotion": promotion,
        "overlays_available": overlays.available(),
    }
