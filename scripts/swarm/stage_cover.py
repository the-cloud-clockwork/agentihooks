"""A bottleneck stage covered by an open phase or task proposes no task and holds no dispatcher seat open.

A cover on a share covers every stage inside it; a cover on a stage also frees its share's dispatcher seat but never
silences a sibling stage's remedy. It lapses, and is
forgotten, the first time `covered` reads it after that phase or task is done, out of scope or gone from a ledger
that still lists others of its kind; a read with none of its kind keeps the cover, so an empty read never erases one.
A new cover on a stage replaces the old one.
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
NOT_OPEN = "{item} is not an open phase or task in this ledger; name one as phases/ID or tasks/ID"
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


def _related(name: str, share: bool) -> list[str]:
    shares = [found for found, members in SHARE_STAGES.items() if name in members]
    return sorted({name, *(SHARE_STAGES.get(name, ()) if share else ()), *shares})


def _named(store: RedisStore, slug: str) -> set[str]:
    return {
        *SHARE_STAGES,
        *(stage for members in SHARE_STAGES.values() for stage in members),
        *standing.read(store.redis, slug),
    }


def cover(store: RedisStore, slug: str, doc: dict, request: Request, now_ms: int) -> dict:
    named, spoken = _named(store, slug), request.stage.strip()
    stage = spoken if spoken in named else spoken.lower().replace(" ", "_").replace("-", "_")
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


def covered(store: RedisStore, slug: str, doc: dict, name: str, share: bool = False) -> bool:
    key, names = store.key(slug, KEY), _related(name, share)
    held = False
    for stage in names:
        raw = store.redis.hget(key, stage)
        if raw is None:
            continue
        item = json.loads(raw)["item"]
        row = _row(doc, item)
        if (row is None and doc.get(item.partition("/")[0])) or (row is not None and _closed(row, item)):
            _lapse(store.redis, key, stage, raw)
        else:
            held = True
    return held


def _lapse(redis, key: str, stage: str, raw: str) -> None:
    def apply(pipe):
        if pipe.hget(key, stage) == raw:
            pipe.multi()
            pipe.hdel(key, stage)

    redis.transaction(apply, key)
