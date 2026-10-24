import json
from collections.abc import Iterable
from dataclasses import asdict, dataclass, replace

from scripts.swarm.store import RedisStore

KEY = "terminals"
LIVE, CACHED, UNVERIFIED = "live", "cached", "unverified"
FRESH_MS = 30_000


@dataclass(frozen=True)
class View:
    """checked_at is the latest check of any outcome; live_at is the latest check that confirmed the pane and its
    conversation. A view carries no screen text."""

    agent: str
    machine: str
    pane_id: str
    state: str
    checked_at: int = 0
    live_at: int = 0
    reason: str = ""
    fresh_ms: int = FRESH_MS


def judged(view: View, now: int) -> View:
    if view.state == LIVE and view.live_at == view.checked_at and abs(now - view.checked_at) <= view.fresh_ms:
        return view
    if view.live_at:
        return replace(view, state=CACHED, reason=view.reason or "stale")
    return replace(view, state=UNVERIFIED, reason=view.reason or "never_live")


def publish(store: RedisStore, slug: str, views: Iterable[View]) -> None:
    key = store.key(slug, KEY)
    rows = {view.agent: json.dumps(asdict(view)) for view in views}
    with store.redis.pipeline() as pipe:
        pipe.delete(key)
        if rows:
            pipe.hset(key, mapping=rows)
        pipe.execute()


def _row(agent: str, raw: str, now: int) -> dict:
    try:
        return asdict(judged(View(**json.loads(raw)), now))
    except (ValueError, TypeError):
        return {"agent": agent, "state": UNVERIFIED, "reason": "unreadable"}


def read(store: RedisStore, slug: str, now: int) -> dict[str, dict]:
    rows = store.redis.hgetall(store.key(slug, KEY))
    return {agent: _row(agent, raw, now) for agent, raw in sorted(rows.items())}
