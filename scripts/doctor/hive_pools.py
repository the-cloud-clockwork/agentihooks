"""Worker hives of one measured shape form a pool; a release roll that drains every hive of a pool at once stops
its launches, so a pool with no ready hive past the threshold is a finding."""

import json

from scripts.hive import placement, placement_waits, registry
from scripts.swarm.health.findings import Finding
from scripts.swarm.store import RedisStore

MINUTE_MS = 60_000
DOWN_MS = 10 * MINUTE_MS
STATE = "doctor-hive-pools"


def pool(measured: dict) -> str:
    return f"{measured['cpus']}cpu-{measured.get('pod_total_mb', measured['total_mb'])}mb"


def ready(hive: dict, measured: dict | None, now_ms: int) -> bool:
    return measured is not None and registry.live(hive, now_ms) and hive.get("draining") != placement.YES


def observe(state: dict, hives: list[dict], readings: dict, refused: list[dict], now_ms: int) -> dict:
    """Hives keep the pool they were last measured in, so a hive that stops publishing stays counted as down."""
    known = state.get("members", {})
    members, up = {}, set()
    for hive in hives:
        if hive["ui"] == placement.YES:
            continue
        measured = readings.get(hive["id"])
        name = pool(measured) if measured is not None else known.get(hive["id"])
        if name is None:
            continue
        members[hive["id"]] = name
        if ready(hive, measured, now_ms):
            up.add(name)
    down = {}
    for name in sorted(set(members.values()) - up):
        entry = state.get("down", {}).get(name, {"since": now_ms, "refused": []})
        fresh = [row["id"] for row in refused if row["seen_at"] >= entry["since"]]
        down[name] = {
            "since": entry["since"],
            "hives": sorted(hive_id for hive_id, member in members.items() if member == name),
            "refused": sorted({*entry["refused"], *fresh}),
        }
    return {"members": members, "down": down}


def findings(state: dict, now_ms: int) -> list[Finding]:
    found = []
    for name, entry in state["down"].items():
        if now_ms - entry["since"] < DOWN_MS:
            continue
        minutes = (now_ms - entry["since"]) // MINUTE_MS
        found.append(
            Finding(
                "hive pool down",
                name,
                f"no hive in pool {name} is ready for {minutes} minutes, "
                f"{len(entry['refused'])} tasks refused a launch meanwhile",
                (f"pool {name}", f"down since {entry['since']}", *(f"hive {hive_id}" for hive_id in entry["hives"])),
                "every hive in a pool not ready for ten minutes",
                minutes,
            )
        )
    return found


def refusals(store: RedisStore) -> list[dict]:
    return [
        {"id": f"{slug}/{task}", "seen_at": row["seen_at"]}
        for slug in store.slugs()
        for task, row in placement_waits.read(store, slug).items()
        if placement.HAS_ROOM in row["reason"]
    ]


def read(store: RedisStore, slug: str, now_ms: int) -> list[Finding]:
    from scripts.hive import headroom

    key = store.key(slug, STATE)
    raw = store.redis.get(key)
    hives = registry.hives(store.redis)
    readings = {hive["id"]: headroom.read(store.redis, hive["id"], now_ms) for hive in hives}
    state = observe(json.loads(raw) if raw else {}, hives, readings, refusals(store), now_ms)
    store.redis.set(key, json.dumps(state))
    return findings(state, now_ms)
