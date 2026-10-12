import json

from scripts.inbox.seats import seat_address
from scripts.inbox.store import InboxStore
from scripts.swarm import stages
from scripts.swarm.store import RedisStore

KEY = "top-bottleneck"
ACTIONS = "bottleneck-actions"


def record_action(store: RedisStore, slug: str, stage: str, text: str, at: int) -> None:
    store.redis.hset(store.key(slug, ACTIONS), stage, json.dumps({"text": text, "at": at}))


def read(store: RedisStore, slug: str) -> dict:
    return json.loads(store.redis.get(store.key(slug, KEY)) or "{}")


def report(store: RedisStore, slug: str, now_ms: int) -> dict:
    found = stages.read(store, slug)
    row = found["stages"][0] if found.get("total") else {}
    stage = row.get("stage", "")
    action = json.loads(store.redis.hget(store.key(slug, ACTIONS), stage) or "{}")
    return {
        "stage": stage,
        "label": stage.replace("_", " ") or "none",
        "trend": row.get("trend", "none" if found else "unknown"),
        "seconds": row.get("seconds", 0.0),
        "measured_at": found.get("at", 0),
        "measured": f"{max(0, (now_ms - found['at']) // 60_000)} minutes ago" if found else "never",
        "last_action": action.get("text", "none recorded"),
        "action_at": action.get("at", 0),
    }


def line(found: dict) -> str:
    return (
        f"Top bottleneck {found['label']}; trend {found['trend']}; measured {found['measured']};"
        f" last action {found['last_action']}"
    )


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
