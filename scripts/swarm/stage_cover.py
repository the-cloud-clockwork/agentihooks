"""A bottleneck stage covered by an open phase or task proposes no task and holds no dispatcher seat open.

A cover on a share covers every stage inside it and a cover on a stage covers its share. It lapses, and is
forgotten, the first time `covered` reads it after that phase or task is done or out of scope; an item missing from
the read keeps its cover, so a partial ledger read never erases one. A new cover on a stage replaces the old one.
"""

import json
from dataclasses import dataclass

from scripts.swarm.health import standing
from scripts.swarm.store import RedisStore, SwarmError

KEY = "stage-covers"
KINDS = ("phases", "tasks")
SHARE_STAGES = {
    "engineering": ("build", "seat_wait", "queue_wait", "claim_wait", "launch", "launch_failure"),
    "ci": ("ci_run", "ci_overrun", "ci_queue"),
    "review": ("review", "merge_queue", "intent_fix"),
    "host": ("host_hold",),
    "quota": ("quota_hold",),
}
NOT_OPEN = "{item} is not an open phase or task in this ledger; name one as phases/<id> or tasks/<id>"
NO_REASON = "a stage cover needs a reason"
UNKNOWN = "no bottleneck stage or share is named {stage}; name one of {names}"


@dataclass(frozen=True)
class Request:
    stage: str
    item: str
    reason: str
    by: str


def _row(doc: dict, item: str) -> dict | None:
    kind, _, item_id = item.partition("/")
    return next((row for row in doc.get(kind, []) if row.get("id") == item_id), None) if kind in KINDS else None


def _closed(row: dict, item: str) -> bool:
    done = row.get("state") == "done" if item.startswith("tasks/") else bool(row.get("done"))
    return done or bool(row.get("out_of_scope"))


def _related(name: str) -> list[str]:
    shares = [share for share, members in SHARE_STAGES.items() if name in members]
    return sorted({name, *SHARE_STAGES.get(name, ()), *shares})


def _named(store: RedisStore, slug: str) -> set[str]:
    return {
        *SHARE_STAGES,
        *(stage for members in SHARE_STAGES.values() for stage in members),
        *standing.read(store.redis, slug),
    }


def cover(store: RedisStore, slug: str, doc: dict, request: Request, now_ms: int) -> dict:
    stage, named = request.stage.strip().lower().replace(" ", "_").replace("-", "_"), _named(store, slug)
    if stage not in named:
        raise SwarmError(UNKNOWN.format(stage=request.stage, names=", ".join(sorted(named))))
    if not request.reason.strip():
        raise SwarmError(NO_REASON)
    row = _row(doc, request.item)
    if row is None or _closed(row, request.item):
        raise SwarmError(NOT_OPEN.format(item=request.item))
    found = {"stage": stage, "item": request.item, "reason": request.reason, "by": request.by, "at": now_ms}
    store.redis.hset(store.key(slug, KEY), stage, json.dumps(found))
    return found


def covered(store: RedisStore, slug: str, doc: dict, name: str) -> bool:
    key, names = store.key(slug, KEY), _related(name)
    held = False
    for stage, raw in zip(names, store.redis.hmget(key, names), strict=True):
        if raw is None:
            continue
        item = json.loads(raw)["item"]
        row = _row(doc, item)
        if row is not None and _closed(row, item):
            store.redis.hdel(key, stage)
        else:
            held = True
    return held
