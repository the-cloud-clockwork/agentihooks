"""Focused tasks the claim gate passes that no agent claims: each tick counts them, and a count past three ticks is
a focus unclaimed health finding for the master."""

from typing import Any

from scripts.swarm import freeze
from scripts.swarm.health import focus_check, kept
from scripts.swarm.health.findings import Finding
from scripts.swarm.store import RedisStore

KEY = "focus-unclaimed"
KIND = "focus unclaimed"
TICKS = 3


def read(store: RedisStore, slug: str) -> dict[str, Any]:
    return kept.read(store, slug, KEY)


def observe(store: RedisStore, slug: str, doc: dict[str, Any], at: int) -> dict[str, Any]:
    """Count one more tick for every focused task whose verdict is claimable; any other verdict drops its count."""
    before = read(store, slug).get("tasks", {})
    seen = focus_check.view(doc, freeze.fix_phase(store.config(slug)))
    counted: dict[str, Any] = {}
    records: list[dict[str, Any]] = doc.get("freezes") or []
    for record in records:
        if record["verb"] != "focus":
            continue
        for task in focus_check.under(record["target"], seen):
            if task["id"] in counted or focus_check.verdict(task, seen) != focus_check.CLAIMABLE:
                continue
            previous = before.get(task["id"], {})
            counted[task["id"]] = {
                "ticks": previous.get("ticks", 0) + 1,
                "since": previous.get("since", at),
                "title": task.get("title") or task["id"],
                "focus": record["target"],
            }
    kept.write(store, slug, KEY, at, "tasks", counted)
    return counted


def findings(store: RedisStore, slug: str) -> list[Finding]:
    """A focus unclaimed finding for every focused, claimable task left unclaimed past three ticks."""
    found: list[Finding] = []
    for task_id, row in read(store, slug).get("tasks", {}).items():
        if row["ticks"] <= TICKS:
            continue
        found.append(
            Finding(
                KIND,
                task_id,
                f"focused task {row['title']} has been claimable but unclaimed for {row['ticks']} ticks",
                (f"the focus on {row['focus']} admits it and no agent claimed it",),
                "three ticks",
                row["ticks"],
                evidence_identity=(row["since"],),
            )
        )
    return found
