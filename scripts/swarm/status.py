"""The swarm status report the ledger page reads, built from the swarm store and a ledger state already in hand."""

import json
import os
import time

from scripts.handoff import transfers
from scripts.inbox.store import InboxStore
from scripts.swarm import snapshot
from scripts.swarm.health import activity, checks, verdicts
from scripts.swarm.health import findings as health
from scripts.swarm.store import ASSIST, codex_split
from scripts.swarm.tick import agent_status
from scripts.swarm_ledger import plan_shape


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
        ),
        now_ms(),
        limits.cooldown_minutes * 60_000,
    )


def status_report(store, slug, state):
    config = store.config(slug)
    tasks = state.get("tasks", [])
    return {
        "config": {**config.__dict__, "codex_share": codex_split(config, os.environ)[0]},
        "agents": [
            {
                **a.__dict__,
                "status": agent_status(a),
                "state_since": int(store.redis.hget(store.key(slug, "state-since"), a.name) or 0),
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
        "findings": findings(store, slug, config, tasks, state.get("_meta", {}).get("events", [])),
        "auto_snapshot": auto_snapshot(config),
        "restored": store.restored(slug),
        "transfers": transfers.list_transfers(store, slug),
        "peer": store.peer(slug),
        "plan_shape": plan_shape.report(tasks, config.max_eng),
    }
