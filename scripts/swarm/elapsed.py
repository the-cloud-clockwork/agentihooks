"""Tick thresholds read as time since the event that started them, never as a count of passes.

The old counts assumed a one minute pass. Half a pass of slack absorbs scheduling jitter, so a one minute cadence
keeps the old pass counts and a slower cadence acts at its first pass at or after the same wall time. Event starts
live in their own keys beside the agent row, so the row stays readable by older strict readers.
"""

from typing import Any

PASS_MS = 60_000
SLACK_MS = PASS_MS // 2
DAY_S = 86_400
IDLE_SINCE = "idle-since"


def reached(elapsed_ms: int, limit_ms: int) -> bool:
    """Whether ``elapsed_ms`` since an event has reached ``limit_ms``, within half a pass of scheduling jitter."""
    return elapsed_ms + SLACK_MS >= limit_ms


def passes(elapsed_ms: int) -> int:
    """The one minute passes an observation begun ``elapsed_ms`` ago spans, the first observation counting as one."""
    return max(0, elapsed_ms + SLACK_MS) // PASS_MS + 1


def anchored(now_ms: int, counted: int) -> int:
    """The event start that makes a stored count of ``counted`` passes the next pass's ``counted + 1``."""
    return now_ms - max(0, counted) * PASS_MS


def _key(store: Any, slug: str, kind: str, name: str) -> str:
    return store.key(slug, kind, name)


def read(store: Any, slug: str, kind: str, name: str) -> int | None:
    """The stored start of event ``kind`` for agent ``name``, or None."""
    raw = store.redis.get(_key(store, slug, kind, name))
    return None if raw is None else int(raw)


def start(store: Any, slug: str, kind: str, name: str, now_ms: int, counted: int) -> int:
    """The start of event ``kind``: the stored one while ``counted`` passes run on, else anchored to ``counted``."""
    found = read(store, slug, kind, name) if counted else None
    if found is not None:
        return found
    since = anchored(now_ms, counted)
    store.redis.set(_key(store, slug, kind, name), since, ex=DAY_S)
    return since


def due(store: Any, slug: str, kind: str, name: str, now_ms: int, counted: int, limit_ms: int) -> bool:
    """Whether a pass now would find event ``kind``, ``counted`` passes in, at or past ``limit_ms``."""
    if not counted:
        return False
    found = read(store, slug, kind, name)
    return reached(now_ms - (anchored(now_ms, counted) if found is None else found), limit_ms)


def hold(store: Any, slug: str, kind: str, name: str, counted: int) -> None:
    """Drop the start of a running event ``kind`` so the next pass anchors to its count and held time never counts."""
    if counted:
        store.redis.delete(_key(store, slug, kind, name))
