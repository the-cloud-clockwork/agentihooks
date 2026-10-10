"""Final attempt records outlive runtime deletion; cleanup waits until the outcome and the archived transcript are durable."""

import json
from dataclasses import asdict, dataclass

from scripts.swarm import lease
from scripts.swarm.store import RedisStore, SwarmError

FINAL_STATES = ("completed", "cancelled")
BACKLOG = ("waiting_outcome", "waiting_archive", "ready")


@dataclass(frozen=True)
class Final:
    execution_id: str
    generation: int
    state: str
    transcript_end: int
    outcome_ref: str = ""


def final(store: RedisStore, slug: str, execution_id: str) -> Final | None:
    raw = store.redis.hget(store.key(slug, "attempt-finals"), execution_id)
    return Final(**json.loads(raw)) if raw else None


def finalize(
    store: RedisStore, slug: str, execution_id: str, state: str, transcript_end: int, outcome_ref: str = ""
) -> Final:
    if state not in FINAL_STATES:
        raise SwarmError("an attempt is final only when completed or explicitly cancelled")
    if type(transcript_end) is not int or transcript_end < 0:
        raise SwarmError("the transcript end must be a non negative offset")
    record = store.execution(slug, execution_id)
    current = final(store, slug, execution_id)
    if current and (
        (current.state, current.transcript_end) != (state, transcript_end)
        or outcome_ref
        and current.outcome_ref not in ("", outcome_ref)
    ):
        raise SwarmError("a final attempt record cannot change")
    kept = current.outcome_ref if current else ""
    entry = Final(execution_id, record.generation, state, transcript_end, outcome_ref or kept)
    store.redis.hset(store.key(slug, "attempt-finals"), execution_id, json.dumps(asdict(entry)))
    return entry


def archive_watermark(store: RedisStore, slug: str, execution_id: str) -> int:
    raw = store.redis.hget(store.key(slug, "heartbeats"), execution_id)
    return json.loads(raw)["archive_watermark"] if raw else 0


def waiting(store: RedisStore, slug: str, entry: Final) -> str:
    if not entry.outcome_ref:
        return "waiting_outcome"
    if archive_watermark(store, slug, entry.execution_id) < entry.transcript_end:
        return "waiting_archive"
    return ""


def retain(store: RedisStore, slug: str, entry: Final, removed: dict) -> dict:
    kept = {
        "execution": asdict(store.execution(slug, entry.execution_id)),
        "final": asdict(entry),
        "archive_watermark": archive_watermark(store, slug, entry.execution_id),
        "removed": removed,
        "retained_at_ms": lease.now_ms(store),
    }
    store.redis.hset(store.key(slug, "attempt-retained"), entry.execution_id, json.dumps(kept))
    return kept


def retained(store: RedisStore, slug: str, execution_id: str) -> dict | None:
    raw = store.redis.hget(store.key(slug, "attempt-retained"), execution_id)
    return json.loads(raw) if raw else None


def execution_cleanup_backlog(store: RedisStore, slug: str) -> dict[str, int]:
    counts = dict.fromkeys(BACKLOG, 0)
    done = set(store.redis.hkeys(store.key(slug, "attempt-retained")))
    for raw in store.redis.hvals(store.key(slug, "attempt-finals")):
        entry = Final(**json.loads(raw))
        if entry.execution_id not in done:
            counts[waiting(store, slug, entry) or "ready"] += 1
    return counts
