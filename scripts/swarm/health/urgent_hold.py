"""Urgent tasks the tick cannot claim: each tick records why, and a hold past ten minutes is a health finding.

Each tick also records the urgent tasks under claim with their claimant, which the panic call reads for quiet claims.
"""

import json
from typing import Any

from scripts.swarm import claim_hold, freeze, release_wait, timing
from scripts.swarm.health.findings import MINUTE_MS, Finding
from scripts.swarm.store import RedisStore
from scripts.swarm_ledger.repository import hierarchy

HOLD_MS = 600_000
KEY = "urgent-hold"
RELEASE = "it waits on a release newer than {after_release}"
BLOCKED = "blocked"
BLOCKED_WHY = "its claimant blocked it"
CLAIMED = "claimed"


def read(store, slug: str) -> dict:
    raw = store.redis.get(store.key(slug, KEY))
    return json.loads(raw) if raw else {}


def observe(store: RedisStore, slug: str, doc: dict[str, Any], at: int) -> dict[str, Any]:
    before = read(store, slug).get("tasks", {})
    graph, released = hierarchy.project(doc)[0], release_wait.checker(store.config(slug))
    fix = freeze.fix_phase(store.config(slug))
    rows = {t["id"]: t for t in doc["tasks"]}
    held = {}
    for task in rows.values():
        if task.get("rank") != "urgent" or task.get("state") not in ("open", BLOCKED) or task.get("out_of_scope"):
            continue
        why = (
            BLOCKED_WHY
            if task["state"] == BLOCKED
            else claim_hold.urgent(task, rows, doc, graph, fix) or (not released(task) and RELEASE.format(**task))
        )
        if task.get("merged_into"):
            continue
        previous = before.get(task["id"], {})
        since = previous["since"] if previous.get("why") == why else at
        held[task["id"]] = {"since": since, "why": why, "title": task.get("title") or task["id"]}
    state: dict[str, Any] = {"at": at, "tasks": held}
    if claimed := _claimed(rows):
        state[CLAIMED] = claimed
    timing.keep()
    store.redis.set(store.key(slug, KEY), json.dumps(state))
    return held


def _claimed(rows: dict[str, dict[str, Any]]) -> dict[str, dict[str, str]]:
    return {
        task_id: {"title": task.get("title") or task_id, "claimed_by": task.get("claimed_by", ""), "state": CLAIMED}
        for task_id, task in rows.items()
        if task.get("rank") == "urgent" and task.get("state") == CLAIMED and not task.get("out_of_scope")
    }


def findings(store, slug: str) -> list[Finding]:
    state = read(store, slug)
    found = []
    for task_id, row in state.get("tasks", {}).items():
        held_ms = state["at"] - row["since"]
        if row["why"] or held_ms <= HOLD_MS:
            continue
        minutes = held_ms // MINUTE_MS
        summary = f"urgent task {row['title']} has been claimable but unclaimed for {minutes} minutes"
        found.append(
            Finding(
                "urgent unclaimable",
                task_id,
                summary,
                ("it is claimable but remains unclaimed",),
                "ten minutes",
                minutes,
                evidence_identity=(row["since"],),
            )
        )
    return found
