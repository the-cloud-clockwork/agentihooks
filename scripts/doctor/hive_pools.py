"""Worker hives of one measured shape form a pool; a release roll that drains every hive of a pool at once stops
its launches, so a pool with no ready hive past the threshold is a finding."""

import json
from typing import Any

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


def _latest_drains(hives: list[dict[str, Any]], members: dict[str, str], name: str) -> dict[str, dict[str, Any]]:
    """Each hive of pool name with its newest drain, so a down pool names who drained it, why and by when."""
    found: dict[str, dict[str, Any]] = {}
    for hive in hives:
        drained = [d for d in hive.get("drains", []) if d["draining"] == "yes"]
        if members.get(hive["id"]) == name and drained:
            found[hive["id"]] = drained[-1]
    return found


def _drain_lines(drains: dict[str, dict[str, Any]]) -> list[str]:
    return [
        f"hive {hive_id} draining {d['draining']} by {d['owner']} at {d['at']} "
        f"for {d['reason']}, deadline {d['deadline_at']}"
        for hive_id, d in sorted(drains.items())
    ]


def observe(
    state: dict[str, Any],
    hives: list[dict[str, Any]],
    readings: dict[str, Any],
    refused: list[dict[str, Any]],
    now_ms: int,
) -> dict[str, Any]:
    """Hives keep the pool they were last measured in, so a hive that stops publishing stays counted as down."""
    known = state.get("members", {})
    members: dict[str, str] = {}
    up: set[str] = set()
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
        drains = _latest_drains(hives, members, name)
        down[name] = {
            "since": entry["since"],
            "hives": sorted(hive_id for hive_id, member in members.items() if member == name),
            "refused": sorted({*entry["refused"], *fresh}),
            **({"drains": drains} if drains else {}),
        }
    return {"members": members, "down": down}


def findings(state: dict[str, Any], now_ms: int) -> list[Finding]:
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
                (
                    f"pool {name}",
                    f"down since {entry['since']}",
                    *(f"hive {hive_id}" for hive_id in entry["hives"]),
                    *_drain_lines(entry.get("drains", {})),
                ),
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
