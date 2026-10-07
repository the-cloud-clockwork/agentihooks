from __future__ import annotations

import json
import os

from scripts.inbox import exits
from scripts.inbox.store import CLOSED, InboxStore
from scripts.swarm.store import MASTER, AgentRecord, RedisStore

MINUTES = "AGENTIHOOKS_MASTER_RETIRE_HANDOFF_MINUTES"
REF = "master-retire-handoff"
ASK = (
    "The swarm is retiring you: {reason}. Write your Handoff v2 now with the handoff skill and submit it with "
    "agentihooks swarm {slug} handoff <doc>, then stop; your successor starts from it. "
    "Without a handoff the swarm retires you in {minutes} minutes."
)


def wait_minutes(env=os.environ) -> float:
    return float(env.get(MINUTES, "5"))


def _key(store: RedisStore, slug: str) -> str:
    return store.key(slug, "master-retire-requests")


def hold(store: RedisStore, slug: str, agent: AgentRecord, reason: str, live: bool, now_ms: int) -> str:
    """Empty when the tick may retire the agent now, else the action that holds a live master for its handoff."""
    minutes = wait_minutes()
    if agent.lane != MASTER or not live or agent.state in {"finished", "starting"} or minutes <= 0:
        forget(store, slug, agent.name)
        return ""
    raw = store.redis.hget(_key(store, slug), agent.name)
    if raw is None:
        text = ASK.format(reason=reason, slug=slug, minutes=f"{minutes:g}")
        item = InboxStore(store.redis).send(exits.BY, agent.name, text, ref=REF)
        store.redis.hset(_key(store, slug), agent.name, json.dumps({"at": now_ms, "reason": reason, "item": item.id}))
        return f"asked {agent.name} for a handoff before retiring it: {reason}"
    if now_ms - json.loads(raw)["at"] < minutes * 60 * 1000:
        return f"waiting on {agent.name}'s handoff before retiring it: {reason}"
    forget(store, slug, agent.name)
    return ""


def forget(store: RedisStore, slug: str, name: str) -> bool:
    raw = store.redis.hget(_key(store, slug), name)
    if raw is None:
        return False
    store.redis.hdel(_key(store, slug), name)
    inbox = InboxStore(store.redis)
    item = inbox.get(json.loads(raw)["item"])
    if item.state not in CLOSED:
        inbox.close(item.id, exits.BY, "done", "the master handed off or was retired")
    return True


def reason(store: RedisStore, slug: str, name: str) -> str:
    raw = store.redis.hget(_key(store, slug), name)
    return json.loads(raw)["reason"] if raw else ""
