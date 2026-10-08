"""Each tick recomputes time left and sends the ledger its live slots and CI median when the estimate moved."""

from scripts.swarm import capacity, ci_speed
from scripts.swarm_ledger import ledger_stats

MOVE_MINUTES = 5


def slots(decision: dict) -> int | None:
    if not decision:
        return None
    lanes = decision["configured"]
    running = sum(decision["effective"][lane] - sum(decision["allocation"][lane].values()) for lane in lanes)
    return min(sum(lanes.values()), running + sum(decision["placeable"].values()))


def moved(saved: dict, inputs: dict, result: dict) -> bool:
    before = saved.get("calculation") or {}
    if saved.get("inputs") != inputs or before.get("tiers") != result["tiers"]:
        return True
    shown, now = before.get("minutes"), result["minutes"]
    if shown is None or now is None:
        return shown != now
    return abs(shown - now) >= MOVE_MINUTES


def refresh(slug, store, ledger, runtime, doc, now_ms) -> list[str]:
    writer = getattr(ledger, "time_left", None)
    if writer is None:
        return []
    observed = getattr(runtime, "quota_capacity", None) is not None
    cached = ci_speed.get(store.redis, slug) or {}
    inputs = {"slots": slots(capacity.read(store, slug)) if observed else None, "ci_minutes": cached.get("minutes")}
    meta = doc.get("_meta", {})
    result = ledger_stats.calculate(doc, meta.get("events", []), now_ms, inputs)
    if moved(meta.get("time_left") or {}, inputs, result):
        writer(slug, inputs["slots"], inputs["ci_minutes"])
    return []
