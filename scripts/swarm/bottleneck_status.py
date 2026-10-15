import json

from scripts.inbox.seats import seat_address
from scripts.inbox.store import InboxStore
from scripts.swarm import stages
from scripts.swarm.store import RedisStore

KEY = "top-bottleneck"
ACTIONS = "bottleneck-actions"
WAITS = {
    stages.SEAT_WAIT: "Seat wait {hours:.1f} task hours: ready while every seat of its lane was busy",
    stages.QUEUE_WAIT: "Queue wait {hours:.1f} task hours: ready while a seat was free or behind more tasks than its lane"
    " has seats",
}


def record_action(store: RedisStore, slug: str, stage: str, text: str, at: int) -> None:
    store.redis.hset(store.key(slug, ACTIONS), stage, json.dumps({"text": text, "at": at}))


def read(store: RedisStore, slug: str) -> dict:
    return json.loads(store.redis.get(store.key(slug, KEY)) or "{}")


def report(store: RedisStore, slug: str, now_ms: int) -> dict:
    found = stages.read(store, slug)
    ranked = sorted(
        (row for row in found.get("stages", []) if row["seconds"] > 0),
        key=lambda row: row["seconds"],
        reverse=True,
    )
    row = next((row for row in ranked if row["stage"] != stages.QUEUE_WAIT), {})
    stage = row.get("stage", "")
    action = json.loads(store.redis.hget(store.key(slug, ACTIONS), stage) or "{}")
    result = {
        "stage": stage,
        "label": stage.replace("_", " ") or "none",
        "trend": row.get("trend", "none" if found else "unknown"),
        "seconds": row.get("seconds", 0.0),
        "measured_at": found.get("at", 0),
        "measured": f"{max(0, (now_ms - found['at']) // 60_000)} minutes ago" if found else "never",
        "last_action": action.get("text", "none recorded"),
        "action_at": action.get("at", 0),
        "waits": _waits(found["stages"]) if found else [],
    }
    if ranked and ranked[0]["stage"] == stages.QUEUE_WAIT:
        result["skipped_leader"] = (
            f"Leading stage queue wait {ranked[0]['seconds'] / 3600:.1f} task hours skipped:"
            " tasks beyond the seat limit or ready while a seat was free are excluded from bottleneck ranking"
        )
    return result


def _waits(rows: list) -> list[str]:
    return [
        text.format(hours=sum(row["seconds"] for row in rows if row["stage"] == stage) / 3600)
        for stage, text in WAITS.items()
    ]


def line(found: dict) -> str:
    text = (
        f"Top bottleneck {found['label']}; trend {found['trend']}; measured {found['measured']};"
        f" last action {found['last_action']}"
    )
    return f"{text}; {found['skipped_leader']}" if "skipped_leader" in found else text


def refresh(slug: str, store: RedisStore, now_ms: int) -> list[str]:
    previous = read(store, slug)
    found = report(store, slug, now_ms)
    changed = found["stage"] != previous.get("stage", "")
    transition = previous.get("transition", 0) + int(changed)
    snapshot = json.dumps({**found, "transition": transition})

    def persist(pipe):
        pipe.set(store.key(slug, KEY), snapshot)

    if changed:
        InboxStore(store.redis, persist=persist).send(
            "swarm",
            seat_address(slug, "master"),
            line(found),
            ref=f"top-bottleneck:{slug}:{transition}",
        )
    else:
        persist(store.redis)
    return [line(found)] if changed else []
