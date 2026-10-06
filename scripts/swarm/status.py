"""The swarm status report the ledger page reads, built from the swarm store and a ledger state already in hand."""

import json
import os
import time
from datetime import date, datetime

from scripts.agents_quota import page_quota
from scripts.gates import lift, progress
from scripts.gates import log as gate_log
from scripts.gates.talk import WORKER_LANES
from scripts.handoff import transfers
from scripts.inbox.store import InboxStore
from scripts.swarm import snapshot
from scripts.swarm.health import activity, checks, verdicts
from scripts.swarm.health import findings as health
from scripts.swarm.store import ASSIST, codex_split
from scripts.swarm.tick import agent_status
from scripts.swarm_ledger import plan_shape

DEFAULT_COMPACT_LIMIT = 600


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
    rows, limits = [a.__dict__ for a in store.agents(slug)], health.limits()
    return verdict_store(store, slug).visible(
        health.findings(
            {"tasks": tasks, "_meta": {"events": events}},
            rows,
            activity.counts(slug),
            now_ms(),
            limits,
            checks.waiting(
                rows,
                tasks,
                limits,
                checks.cached(store.redis, store.key(slug, "checks"), approval=config.autonomy == ASSIST),
            ),
            checks.green(tasks, checks.cached_green(store.redis, store.key(slug, "checks"))),
            talk_since_outcome(store, slug, rows),
        ),
        now_ms(),
        limits.cooldown_minutes * 60_000,
    )


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
                "successor": row.get("successor") or agent.get("name", ""),
                "awaiting": awaiting,
            }
        )
    return rows


def status_report(store, slug, state):
    config = store.config(slug)
    tasks = state.get("tasks", [])
    events = state.get("_meta", {}).get("events", [])
    agents = [a.__dict__ for a in store.agents(slug)]
    handed = transfers.list_transfers(store, slug)
    active = lift.active(gate_log.recent(slug, limit=None), now_ms())
    return {
        "config": {**config.__dict__, "codex_share": codex_split(config, os.environ)[0]},
        "agents": [
            {
                **a.__dict__,
                "status": agent_status(a),
                "state_since": int(store.redis.hget(store.key(slug, "state-since"), a.name) or 0),
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
        "auto_snapshot": auto_snapshot(config),
        "restored": store.restored(slug),
        "transfers": handed,
        "handoffs": handoff_rows(handed, agents),
        "peer": store.peer(slug),
        "plan_shape": plan_shape.report(tasks, config.max_eng),
        "compact_limit": compact_limit(config),
        "done_today": done_today(tasks, events, local_midnight_ms()),
        "doctor": doctor_report(store, slug),
        "quota": page_quota(),
        "gates": gate_log.recent(slug),
    }
