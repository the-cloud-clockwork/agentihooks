"""Operator orders nobody works on: the slow lane notes when each open order's unfinished tasks first had nothing
claimed or in a pull request, and past thirty minutes that is an order waiting health finding for the master."""

import json
from typing import Any

from scripts.swarm import freeze
from scripts.swarm.health import focus_check, order_check
from scripts.swarm.health.findings import MINUTE_MS, Finding
from scripts.swarm.store import RedisStore

KEY = "order-waiting"
KIND = "order waiting"
MINUTES = 30
UNCLAIMED = "every open task under it is claimable and no agent claimed one"


def read(store: RedisStore, slug: str) -> dict[str, Any]:
    client: Any = store
    raw: str | None = client.redis.get(store.key(slug, KEY))
    return json.loads(raw) if raw else {}


def observe(store: RedisStore, slug: str, doc: dict[str, Any], at: int) -> dict[str, Any]:
    """Note each open order with unfinished tasks and none claimed or in a pull request, keeping the time it was
    first seen so; any other order drops out."""
    before = read(store, slug).get("orders", {})
    waiting: dict[str, Any] = {}
    for entry in order_check.read(doc, freeze.fix_phase(store.config(slug))):
        if not entry["tasks"] or entry["busy"]:
            continue
        waiting[entry["item"]] = {
            "since": before.get(entry["item"], {}).get("since", at),
            "title": entry["title"],
            "unfinished": len(entry["tasks"]),
            "claimable": entry["claimable"],
            "held": sorted({task["verdict"] for task in entry["tasks"]} - {focus_check.CLAIMABLE}),
        }
    client: Any = store
    client.redis.set(store.key(slug, KEY), json.dumps({"at": at, "orders": waiting}))
    return waiting


def findings(store: RedisStore, slug: str) -> list[Finding]:
    """An order waiting finding for every order whose unfinished tasks had nothing claimed or in a pull request for
    more than thirty minutes."""
    noted = read(store, slug)
    found: list[Finding] = []
    for item, row in noted.get("orders", {}).items():
        minutes = (noted["at"] - row["since"]) // MINUTE_MS
        if minutes <= MINUTES:
            continue
        found.append(
            Finding(
                KIND,
                item,
                f"operator order {item} {row['title']}: {row['unfinished']} unfinished, {row['claimable']} claimable, "
                f"none claimed or in a pull request for {minutes} minutes",
                tuple(row["held"]) or (UNCLAIMED,),
                "thirty minutes",
                minutes,
                evidence_identity=(row["since"],),
            )
        )
    return found
