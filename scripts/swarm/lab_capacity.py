"""The CI lab capacity a swarm tick reads from the lab scheduler, keeps in Redis and ships to ClickHouse."""

import json
from typing import Any
from urllib.request import urlopen

from scripts.swarm import metrics_outbox

__all__ = ["KEY", "TABLE", "TIMEOUT_S", "capture", "read", "rows", "status"]

KEY = "lab-capacity"
TIMEOUT_S = 3
PATH = dict.fromkeys(("plan", "phase", "slice", "task"), "")
NO_WAIT = -1.0
TABLE = metrics_outbox.Table(
    "lab_capacity",
    (
        ("class", "String"),
        ("pending", "Int64"),
        ("running", "Int64"),
        ("predicted_wait_s", "Float64"),
        ("shed", "Int64"),
        ("slots_total", "Int64"),
        ("slots_busy", "Int64"),
        ("utilisation", "Float64"),
        ("budget_fraction", "Float64"),
    ),
)


def read(url: str, timeout_s: float = TIMEOUT_S) -> dict[str, Any]:
    """The capacity document the lab scheduler at ``url`` serves at GET /capacity."""
    with urlopen(f"{url.rstrip('/')}/capacity", timeout=timeout_s) as reply:
        doc = json.loads(reply.read())
    if not isinstance(doc, dict) or not isinstance(doc.get("classes"), dict):
        raise ValueError("lab capacity document carries no classes")
    return doc


def rows(slug: str, doc: dict[str, Any], now_ms: int) -> list[dict[str, Any]]:
    """One lab_capacity row per scheduling class of capacity document ``doc``."""
    slots = doc["slots"]
    return [
        {
            "event_id": f"lab-capacity:{slug}:{now_ms}:{name}",
            "ledger": slug,
            "ts_ms": now_ms,
            **PATH,
            "class": name,
            "pending": int(entry["pending"]),
            "running": int(entry["running"]),
            "predicted_wait_s": NO_WAIT if entry["predicted_wait_s"] is None else float(entry["predicted_wait_s"]),
            "shed": int(name in doc["shed"]),
            "slots_total": int(slots["total"]),
            "slots_busy": int(slots["busy"]),
            "utilisation": float(doc["utilisation"]),
            "budget_fraction": float(doc["budget_fraction"]),
        }
        for name, entry in sorted(doc["classes"].items())
    ]


def status(store: Any, slug: str) -> dict[str, Any] | None:
    """The lab block of swarm ``slug``: the last capacity read and the last error, or None before any read."""
    raw = store.redis.get(store.key(slug, KEY))
    return json.loads(raw) if raw else None


def capture(store: Any, slug: str, url: str, now_ms: int) -> list[dict[str, Any]]:
    """Read the capacity at ``url`` into the swarm's lab block and return its rows; a failed read keeps the last
    capacity and records the error."""
    if not url:
        return []
    last = status(store, slug) or {}
    try:
        doc = read(url)
        found = rows(slug, doc, now_ms)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        block = {**last, "url": url, "error": str(exc), "error_at": now_ms}
        store.redis.set(store.key(slug, KEY), json.dumps(block))
        return []
    block = {"url": url, "at": now_ms, "capacity": doc, "error": "", "error_at": 0}
    store.redis.set(store.key(slug, KEY), json.dumps(block))
    return found
