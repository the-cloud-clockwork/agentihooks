"""A bottleneck stage covered by an open phase or task proposes no task and holds no dispatcher seat open; the cover
lapses, and is forgotten, the first time it is read after that phase or task closes.
"""

import json
from dataclasses import dataclass

from scripts.swarm import dispatcher
from scripts.swarm.store import RedisStore, SwarmError

KEY = "stage-covers"
KINDS = ("phases", "tasks")
NOT_OPEN = "{item} is not an open phase or task in this ledger; name one as phases/<id> or tasks/<id>"
NO_REASON = "a stage cover needs a reason"


def _open(doc: dict, item: str) -> bool:
    kind, _, item_id = item.partition("/")
    if kind not in KINDS:
        return False
    row = next((row for row in doc.get(kind, []) if row.get("id") == item_id), None)
    if row is None:
        return False
    if kind == "tasks":
        return dispatcher._open(row)
    return not row.get("done") and not row.get("out_of_scope")


@dataclass(frozen=True)
class Request:
    stage: str
    item: str
    reason: str
    by: str


def cover(store: RedisStore, slug: str, doc: dict, request: Request, now_ms: int) -> dict:
    if not request.reason.strip():
        raise SwarmError(NO_REASON)
    if not _open(doc, request.item):
        raise SwarmError(NOT_OPEN.format(item=request.item))
    stage = request.stage.strip().replace(" ", "_")
    row = {"stage": stage, "item": request.item, "reason": request.reason, "by": request.by, "at": now_ms}
    store.redis.hset(store.key(slug, KEY), stage, json.dumps(row))
    return row


def read(store: RedisStore, slug: str) -> dict:
    return {stage: json.loads(raw) for stage, raw in store.redis.hgetall(store.key(slug, KEY)).items()}


def covered(store: RedisStore, slug: str, doc: dict, stage: str) -> bool:
    raw = store.redis.hget(store.key(slug, KEY), stage)
    if raw is None:
        return False
    if _open(doc, json.loads(raw)["item"]):
        return True
    store.redis.hdel(store.key(slug, KEY), stage)
    return False
