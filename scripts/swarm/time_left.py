"""Each tick sends the ledger its live slots and CI median, so time left and its refresh age stay current."""

import sqlite3
from typing import Any

from scripts.swarm import capacity, ci_speed, freeze
from scripts.swarm.ledger_client import LedgerRefused

WATCHED_KEPT = 100
WATCHED_LENGTH = 2000


def slots(decision: dict) -> int | None:
    if not decision:
        return None
    lanes = decision["configured"]
    running = sum(decision["effective"][lane] - sum(decision["allocation"][lane].values()) for lane in lanes)
    return min(sum(lanes.values()), running + sum(decision["placeable"].values()))


def inputs_of(store, slug, runtime) -> dict:
    observed = getattr(runtime, "quota_capacity", None) is not None
    cached = ci_speed.get(store.redis, slug) or {}
    return {"slots": slots(capacity.read(store, slug)) if observed else None, "ci_minutes": cached.get("minutes")}


def focus(config, doc: dict) -> dict:
    watched = [name[:WATCHED_LENGTH] for name in (doc.get(freeze.WATCHED) or [])[:WATCHED_KEPT]]
    return {key: value for key, value in (("fix_phase", freeze.fix_phase(config)), ("watched", watched)) if value}


def refresh(slug: str, store: Any, ledger: Any, runtime: Any, config: Any, doc: dict[str, Any]) -> list[str]:
    writer = getattr(ledger, "time_left", None)
    if writer is not None:
        inputs = inputs_of(store, slug, runtime)
        try:
            writer(slug, inputs["slots"], inputs["ci_minutes"], **focus(config, doc))
        except sqlite3.DatabaseError as exc:
            raise LedgerRefused(f"time left write failed: {exc}") from exc
    return []
