import json

from scripts.hive.placement import HAS_ROOM, HIVE, NEEDS_WORKSTATION, NO_UI_HIVE
from scripts.swarm.health.findings import Finding
from scripts.swarm.store import RedisStore

WAIT_MS = 30 * 60_000
FRESH_MS = 5 * 60_000
WAITS = "hive-placement-waits"


def read(store: RedisStore, slug: str) -> dict:
    return {task: json.loads(raw) for task, raw in store.redis.hgetall(store.key(slug, WAITS)).items()}


def observe(store: RedisStore, slug: str, lane: str, task: str, reason: str, now: int, workstation: str = "") -> None:
    from redis.exceptions import WatchError

    key = store.key(slug, WAITS)
    with store.redis.pipeline() as pipe:
        while True:
            try:
                pipe.watch(key)
                raw = pipe.hget(key, task)
                previous = json.loads(raw) if raw else {}
                started = now
                if reason and previous.get("reason") == reason and previous.get("lane") == lane:
                    started = previous["started_at"]
                pipe.multi()
                if reason:
                    row = {"lane": lane, "reason": reason, "started_at": started, "seen_at": now}
                    if workstation:
                        row["workstation"] = workstation
                    pipe.hset(key, task, json.dumps(row))
                else:
                    pipe.hdel(key, task)
                pipe.execute()
                return
            except WatchError:
                continue


def prune(store: RedisStore, slug: str, tasks: list[dict]) -> None:
    active = {task["id"] for task in tasks if task.get("state") not in ("done", "cancelled")}
    for task in read(store, slug).keys() - active:
        store.redis.hdel(store.key(slug, WAITS), task)


def workstation_waiting(store: RedisStore, machine: str, now: int, own: str) -> int:
    return sum(
        1
        for slug in store.slugs()
        for task, row in read(store, slug).items()
        if row.get("workstation") == machine
        and now - row["seen_at"] < FRESH_MS
        and (
            (row["reason"].startswith(f"no {HIVE} ") and f" {HAS_ROOM}" in row["reason"])
            or row["reason"] == NEEDS_WORKSTATION
        )
        and f"{slug}/{task}" != own
    )


def findings(store: RedisStore, slug: str, now: int) -> list[Finding]:
    missing = {}
    for task, row in read(store, slug).items():
        reason = row["reason"]
        if reason == NO_UI_HIVE:
            wanted = "ui"
        elif reason.startswith("no live hive takes "):
            wanted = reason.removeprefix("no live hive takes ")
        else:
            continue
        age = now - row["started_at"]
        if age < WAIT_MS:
            continue
        found = Finding(
            "missing hive",
            wanted,
            f"Wanted {wanted} hive has been missing for thirty minutes",
            (f"Task {task} waiting since {row['started_at']}: {reason}",),
            "thirty minutes",
            age,
        )
        if wanted not in missing or age > missing[wanted].measure:
            missing[wanted] = found
    return [missing[wanted] for wanted in sorted(missing)]
