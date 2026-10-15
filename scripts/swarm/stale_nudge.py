"""Stale check run by the swarm tick: one inbox item per stale agent per ten minute window, never a typed prompt.

An agent is stale when its last session heartbeat is over ten minutes old and no named wait holds, or when its pane
shows a provider error. The item id comes from the agent and the window, so repeated ticks inside one window send it
once. The inbox channel delivers it to Claude; the Codex push pump starts a new app server turn for it.
"""

import hashlib

from scripts.inbox.store import DuplicateItem
from scripts.swarm import idle

LIMIT_MS = 10 * 60_000
SENDER = "swarm"
REF = "stale-nudge"
IDLE_REASON = "no activity from you for over ten minutes"
ERROR_REASON = "your pane shows a provider error"
TEXT = (
    "Stale check on swarm {slug}: {reason}. Pick task {task} back up now: read progress.md in your task folder and "
    "your inbox, then take the next step. If you are waiting on checks, a merge, a reply or another task, declare it "
    "with agentihooks swarm {slug} wait --on and the thing that ends it. Close this message with agentihooks msg close "
    "and its id, done."
)


def last_activity(redis, slug, agent):
    beat = idle.heartbeat(redis, slug, agent.name) or {}
    return max(int(beat.get("at") or 0), agent.started_at or 0)


def stale(last_activity_ms, wait, provider_error, now_ms):
    if provider_error:
        return True
    if wait and wait.get("until", 0) > now_ms and idle.named(wait):
        return False
    return now_ms - last_activity_ms > LIMIT_MS


def item_id(slug, name, now_ms):
    return hashlib.sha256(f"{REF}:{slug}:{name}:{now_ms // LIMIT_MS}".encode()).hexdigest()[:12]


def is_nudge(item):
    return item.sender == SENDER and item.ref.startswith(f"{REF}:")


def nudge(inbox, slug, agent, provider_error, now_ms):
    redis = inbox.redis
    if not stale(last_activity(redis, slug, agent), idle.wait(redis, slug, agent.name), provider_error, now_ms):
        return []
    reason = ERROR_REASON if provider_error else IDLE_REASON
    text, nudge_id = TEXT.format(slug=slug, reason=reason, task=agent.task), item_id(slug, agent.name, now_ms)
    try:
        item = inbox.send(SENDER, agent.name, text, ref=f"{REF}:{nudge_id}", task=agent.task, item_id=nudge_id)
    except DuplicateItem:
        return []
    return [f"nudged stale {agent.name} through its inbox as message {item.id}"]
