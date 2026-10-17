"""Urgent tasks the tick cannot claim: each tick records why, and a hold past ten minutes is a health finding."""

import json

from scripts.swarm import claim_hold, freeze, timing
from scripts.swarm.health.findings import MINUTE_MS, Finding
from scripts.swarm_ledger.repository import hierarchy

HOLD_MS = 600_000
KEY = "urgent-hold"


def read(store, slug: str) -> dict:
    raw = store.redis.get(store.key(slug, KEY))
    return json.loads(raw) if raw else {}


def observe(store, slug: str, doc: dict, at: int) -> dict:
    before = read(store, slug).get("tasks", {})
    fix, graph = freeze.fix_phase(store.config(slug)), hierarchy.project(doc)[0]
    rows = {t["id"]: t for t in doc.get("tasks", [])}
    held = {}
    for task in rows.values():
        if task.get("rank") != "urgent" or task.get("state") != "open" or task.get("out_of_scope"):
            continue
        if task.get("merged_into") or not (why := claim_hold.hold(task, rows, doc, graph, fix)):
            continue
        since = before.get(task["id"], {}).get("since", at)
        held[task["id"]] = {"since": since, "why": why, "title": task.get("title") or task["id"]}
    timing.keep()
    store.redis.set(store.key(slug, KEY), json.dumps({"at": at, "tasks": held}))
    return held


def findings(store, slug: str) -> list[Finding]:
    state = read(store, slug)
    found = []
    for task_id, row in state.get("tasks", {}).items():
        held_ms = state["at"] - row["since"]
        if held_ms < HOLD_MS:
            continue
        minutes = held_ms // MINUTE_MS
        summary = f"urgent task {row['title']} has been unclaimable for {minutes} minutes"
        found.append(Finding("urgent unclaimable", task_id, summary, (row["why"],), "ten minutes", minutes))
    return found
