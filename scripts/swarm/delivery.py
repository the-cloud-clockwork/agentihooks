"""Swarm chat delivery: push each message into its recipients' herdr panes once they are idle.

A busy agent's message waits in a Redis outbox that every tick flushes, so no process watches for it.
"""

import json
import time
import uuid

READY = ("idle", "done")
EXPIRE_MS = 30 * 60 * 1000
FLUSH_LOCK_MS = 120 * 1000


class HerdrMessenger:
    def __init__(self, herdr=None):
        from scripts.swarm import runtime

        self.herdr = herdr or runtime.herdr_call
        self.target = runtime.herdr_target

    def agent_status(self, agent):
        result = self.herdr(["agent", "get", self.target(agent.name)])
        found = result.get("agent", result)
        return found.get("agent_status") or found.get("status") or "unknown"

    def prompt(self, agent, text):
        self.herdr(["agent", "prompt", self.target(agent.name), text])


def _now():
    return int(time.time() * 1000)


def recipients(store, slug, to, sender):
    agents = [a for a in store.agents(slug) if a.name != sender and a.state != "finished"]
    if to in ("", "all"):
        return agents
    return [a for a in agents if to in (a.lane, a.name)]


def send(store, slug, text, sender, to, herdr, now_ms=None):
    outbox, at = store.key(slug, "outbox"), now_ms or _now()
    found = recipients(store, slug, to, sender)
    for agent in found:
        store.redis.rpush(outbox, json.dumps({"to": agent.name, "at": at, "text": f"[swarm chat] {sender}: {text}"}))
    flush(store, slug, herdr, now_ms=at)
    return [a.name for a in found]


def flush(store, slug, herdr, now_ms=None):
    """Deliver what can be delivered; one flusher at a time, each item taken atomically."""
    lock, token = store.key(slug, "flush-lock"), uuid.uuid4().hex
    if not store.redis.set(lock, token, nx=True, px=FLUSH_LOCK_MS):
        return []
    try:
        return _drain(store, slug, herdr, now_ms or _now())
    finally:
        if store.redis.get(lock) == token:
            store.redis.delete(lock)


def _drain(store, slug, herdr, now_ms):
    outbox = store.key(slug, "outbox")
    agents = {a.name: a for a in store.agents(slug) if a.state != "finished"}
    sent, keep = [], []
    for _ in range(store.redis.llen(outbox)):
        raw = store.redis.lpop(outbox)
        if raw is None:
            break
        item = json.loads(raw)
        agent = agents.get(item["to"])
        if agent is None or now_ms - item.get("at", now_ms) > EXPIRE_MS:
            continue
        try:
            if herdr.agent_status(agent) in READY:
                herdr.prompt(agent, item["text"])
                sent.append(agent.name)
                continue
        except Exception:
            pass
        keep.append(raw)
    if keep:
        store.redis.rpush(outbox, *keep)
    return sent


def latest_at(entries):
    return max((e.get("at", 0) for e in entries), default=0)


def start_cursor(store, slug, entries):
    store.redis.set(store.key(slug, "chat-cursor"), latest_at(entries))


def relay_operator_chat(store, slug, entries, herdr):
    """Send every operator chat entry newer than the swarm's cursor; a leading @name, @eng or @ci addresses it."""
    cursor_key = store.key(slug, "chat-cursor")
    cursor = int(store.redis.get(cursor_key) or 0)
    fresh = [
        e for e in entries if e.get("by", "operator") == "operator" and e.get("at", 0) > cursor and not e.get("deleted")
    ]
    for entry in sorted(fresh, key=lambda e: e["at"]):
        text, to = entry.get("text", ""), ""
        if text.startswith("@") and " " in text:
            to, text = text[1:].split(" ", 1)
        if not send(store, slug, text, sender="operator", to=to, herdr=herdr) and to:
            send(store, slug, f"(to {to}, who is not in the swarm) {text}", sender="operator", to="", herdr=herdr)
        store.redis.set(cursor_key, entry["at"])
    return len(fresh)
