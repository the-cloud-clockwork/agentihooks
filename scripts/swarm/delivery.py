"""Swarm chat delivery: push each message into its recipients' herdr panes once they are idle.

A busy agent's message waits in a Redis outbox; every tick flushes it, so no process watches for it.
"""

import json

from scripts.herdr_host import HerdrError

READY = ("idle", "done")


class HerdrMessenger:
    def agent_status(self, pane_id):
        import os

        from scripts.herdr_host import _cli

        result = _cli(["agent", "get", pane_id], dict(os.environ))
        agent = result.get("agent", result)
        return agent.get("agent_status") or agent.get("status") or "unknown"

    def prompt(self, pane_id, text):
        import os

        from scripts.herdr_host import _cli

        _cli(["agent", "prompt", pane_id, text], dict(os.environ))


def recipients(store, slug, to, sender):
    agents = [a for a in store.agents(slug) if a.name != sender and a.state != "finished"]
    if to in ("", "all"):
        return agents
    return [a for a in agents if to in (a.lane, a.name)]


def send(store, slug, text, sender, to, herdr):
    outbox = store.key(slug, "outbox")
    for agent in recipients(store, slug, to, sender):
        store.redis.rpush(outbox, json.dumps({"to": agent.name, "text": f"[swarm chat] {sender}: {text}"}))
    return flush(store, slug, herdr)


def flush(store, slug, herdr):
    outbox = store.key(slug, "outbox")
    panes = {a.name: a.pane_id for a in store.agents(slug) if a.state != "finished"}
    sent, keep = [], []
    for raw in store.redis.lrange(outbox, 0, -1):
        item = json.loads(raw)
        pane = panes.get(item["to"])
        if not pane:
            continue
        try:
            if herdr.agent_status(pane) in READY:
                herdr.prompt(pane, item["text"])
                sent.append(item["to"])
                continue
        except HerdrError:
            pass
        keep.append(raw)
    with store.redis.pipeline() as pipe:
        pipe.delete(outbox)
        if keep:
            pipe.rpush(outbox, *keep)
        pipe.execute()
    return sent


def relay_operator_chat(store, slug, entries, herdr):
    """Queue every operator chat entry newer than the swarm's cursor; a leading @name, @eng or @ci addresses it."""
    cursor_key = store.key(slug, "chat-cursor")
    cursor = int(store.redis.get(cursor_key) or 0)
    fresh = [
        e for e in entries if e.get("by", "operator") == "operator" and e.get("at", 0) > cursor and not e.get("deleted")
    ]
    for entry in fresh:
        text, to = entry.get("text", ""), ""
        if text.startswith("@") and " " in text:
            to, text = text[1:].split(" ", 1)
        send(store, slug, text, sender="operator", to=to, herdr=herdr)
    if fresh:
        store.redis.set(cursor_key, max(e["at"] for e in fresh))
    return len(fresh)
