import json

from scripts.inbox.seats import seat_address
from scripts.inbox.store import InboxStore
from scripts.swarm.health import standing
from scripts.swarm.store import RedisStore

KEY = "top-bottleneck"
ACTIONS = "bottleneck-actions"


def record_action(store: RedisStore, slug: str, stage: str, text: str, at: int) -> None:
    store.redis.hset(store.key(slug, ACTIONS), stage, json.dumps({"text": text, "at": at}))


def read(store: RedisStore, slug: str) -> dict:
    return json.loads(store.redis.get(store.key(slug, KEY)) or "{}")


def trend(row: dict, prior: dict) -> str:
    if not prior or row["unit"] != prior["unit"]:
        return "unknown"
    if row["updated_at"] == prior["updated_at"]:
        return prior["trend"]
    if row["value"] == prior["value"]:
        return "steady"
    return "rising" if row["value"] > prior["value"] else "falling"


def observations(store: RedisStore, slug: str, at: int, previous: dict) -> dict:
    old = previous.get("readings", {})
    return {
        stage: {**row, "trend": trend(row, old.get(stage, {}))}
        for stage, row in standing.read(store.redis, slug).items()
        if at - row["updated_at"] < standing.STALE_MS and row["threshold"] > 0
    }


def top(store: RedisStore, slug: str, rows: dict) -> dict:
    stage = min(rows, key=lambda name: (-rows[name]["value"] / rows[name]["threshold"], name), default="")
    action = json.loads(store.redis.hget(store.key(slug, ACTIONS), stage) or "{}")
    return {
        **rows.get(stage, {}),
        "stage": stage,
        "label": stage.replace("_", " ") or "none",
        "trend": rows[stage]["trend"] if stage else "unknown",
        "last_action": action.get("text", "none recorded"),
        "action_at": action.get("at", 0),
    }


def report(store: RedisStore, slug: str, at: int) -> dict:
    return top(store, slug, observations(store, slug, at, read(store, slug)))


def line(found: dict) -> str:
    return f"Top bottleneck {found['label']}; trend {found['trend']}; last action {found['last_action']}"


def refresh(slug: str, store: RedisStore, at: int) -> list[str]:
    previous = read(store, slug)
    rows = observations(store, slug, at, previous)
    found = top(store, slug, rows)
    changed = found["stage"] != previous.get("stage", "")
    transition = previous.get("transition", 0) + int(changed)
    snapshot = json.dumps({**found, "readings": rows, "transition": transition})

    def persist(pipe):
        pipe.set(store.key(slug, KEY), snapshot)

    if changed:
        InboxStore(store.redis).send(
            "swarm",
            seat_address(slug, "master"),
            line(found),
            ref=f"top-bottleneck:{slug}:{transition}",
            persist=persist,
        )
    else:
        persist(store.redis)
    return [line(found)] if changed else []
