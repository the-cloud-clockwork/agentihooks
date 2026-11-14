"""Tick stale check: one inbox item per stale swarm agent per ten minutes of silence, and one cache warm check per
forty minutes of silence inside a declared named wait, never a typed prompt."""

import hashlib

from scripts.inbox.store import DuplicateItem, InboxStore, Item
from scripts.swarm import idle
from scripts.swarm.store import AgentRecord

LIMIT_MS = 10 * 60_000
CACHE_LIMIT_MS = 40 * 60_000
FRESH_MS = 2 * 60_000
SENDER = "swarm"
REF = "stale-nudge"
CACHE_REF = "cache-warm"
LAST = "stale-nudged"
IDLE_REASON = "no activity from you for over ten minutes"
ERROR_REASON = "your pane shows a provider error"
TEXT = (
    "Stale check on swarm {slug}: {reason}. Pick task {task} back up now: read progress.md in your task folder and "
    "your inbox, then take the next step. If you are waiting on checks, a merge, a reply or another task, declare it "
    "with agentihooks swarm {slug} wait --on and the thing that ends it. Close this message with agentihooks msg close "
    "and its id, done."
)
CACHE_TEXT = "Cache warm check on swarm {slug}: reply in one line and keep your wait on {task}; no other action."


def last_activity(beat: dict | None, agent: AgentRecord) -> int:
    return max(int((beat or {}).get("at") or 0), agent.started_at or 0, agent.launched_at)


def stale(beat: dict | None, last_ms: int, wait: dict | None, provider_error: str, now_ms: int) -> bool:
    """A provider error counts unless the heartbeat shows a turn working inside the limit."""
    silent = bool(last_ms) and now_ms - last_ms > LIMIT_MS
    working = bool(beat) and beat.get("state") == idle.WORKING and not silent
    if provider_error and not working:
        return True
    if wait and wait.get("until", 0) > now_ms and idle.named(wait):
        return False
    return silent


def cache_due(last_ms: int, now_ms: int) -> bool:
    """Called only once stale has ruled out a nudge, so past the ten minute limit the agent sits in a named open wait."""
    return bool(last_ms) and now_ms - last_ms > CACHE_LIMIT_MS


def window(last_ms: int, now_ms: int) -> int:
    return (now_ms - last_ms) // LIMIT_MS


def item_id(slug: str, name: str, last_ms: int, now_ms: int) -> str:
    key = f"{REF}:{slug}:{name}:{last_ms}:{window(last_ms, now_ms)}"
    return hashlib.sha256(key.encode()).hexdigest()[:12]


def cache_item_id(slug: str, name: str, last_ms: int, now_ms: int) -> str:
    key = f"{CACHE_REF}:{slug}:{name}:{last_ms}:{(now_ms - last_ms) // CACHE_LIMIT_MS}"
    return hashlib.sha256(key.encode()).hexdigest()[:12]


def is_nudge(item: Item) -> bool:
    return item.sender == SENDER and item.ref.startswith(f"{REF}:")


def interrupts(item: Item, now_ms: int) -> bool:
    """A nudge held past its delivery window may meet a resumed turn, so only a fresh one interrupts."""
    return is_nudge(item) and now_ms - item.created_at <= FRESH_MS


def _send_cache_warm(inbox: InboxStore, slug: str, agent: AgentRecord, last_ms: int, now_ms: int) -> list[str]:
    nudge_id = cache_item_id(slug, agent.name, last_ms, now_ms)
    text = CACHE_TEXT.format(slug=slug, task=agent.task)
    try:
        item = inbox.send(SENDER, agent.name, text, ref=f"{CACHE_REF}:{nudge_id}", task=agent.task, item_id=nudge_id)
    except DuplicateItem:
        return []
    return [f"sent {agent.name} a cache warm check through its inbox as message {item.id}"]


def nudge(inbox: InboxStore, slug: str, agent: AgentRecord, provider_error: str, now_ms: int) -> list[str]:
    redis = inbox.redis
    beat = idle.heartbeat(redis, slug, agent.name)
    last_ms = last_activity(beat, agent)
    wait = idle.wait(redis, slug, agent.name)
    if not stale(beat, last_ms, wait, provider_error, now_ms):
        if cache_due(last_ms, now_ms):
            return _send_cache_warm(inbox, slug, agent, last_ms, now_ms)
        return []
    reason = ERROR_REASON if provider_error else IDLE_REASON
    nudge_id = item_id(slug, agent.name, last_ms, now_ms)
    text = TEXT.format(slug=slug, reason=reason, task=agent.task)
    try:
        item = inbox.send(SENDER, agent.name, text, ref=f"{REF}:{nudge_id}", task=agent.task, item_id=nudge_id)
    except DuplicateItem:
        return []
    redis.set(idle.key(slug, LAST, agent.name), now_ms, ex=idle.BEAT_TTL_S)
    return [f"nudged stale {agent.name} through its inbox as message {item.id}"]


def last(redis, slug: str, name: str) -> int | None:
    raw = redis.get(idle.key(slug, LAST, name))
    return int(raw) if raw else None
