"""Swarm chat over the inbox: addressed swarm chat and operator page lines become inbox items.

The inbox hooks deliver an item at the receiver's next tool call and the tick's wake pass prompts idle panes.
Replies addressed to the operator are posted on the ledger page chat.
"""

import json

from scripts.inbox.store import InboxStore
from scripts.swarm.store import MASTER

READY = ("idle", "done")
OPERATOR = "operator"
LEGACY_PREFIX = "[swarm chat] "


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


def recipients(store, slug, to, sender):
    agents = [a for a in store.agents(slug) if a.name != sender and a.state != "finished"]
    if to in ("", "all"):
        return agents
    return [a for a in agents if to in (a.lane, a.name)]


def send(store, slug, text, sender, to):
    found = recipients(store, slug, to, sender)
    inbox = InboxStore(store.redis)
    for agent in found:
        inbox.send(sender, agent.name, text)
    return [a.name for a in found]


def relay_to_page(inbox, slug, agents, ledger):
    names = {a.name for a in agents}
    shown = 0
    for item in inbox.inbox(OPERATOR):
        if item.state != "pending" or item.sender not in names:
            continue
        ledger.say(slug, item.text, by=item.sender)
        inbox.close(item.id, OPERATOR, "done", "shown on the ledger page")
        shown += 1
    return shown


def migrate_outbox(store, slug, inbox):
    """Move what the retired herdr outbox still holds into the inbox."""
    outbox = store.key(slug, "outbox")
    moved = 0
    while (raw := store.redis.lpop(outbox)) is not None:
        entry = json.loads(raw)
        sender, text = "swarm", entry["text"]
        if text.startswith(LEGACY_PREFIX) and ": " in text:
            sender, text = text[len(LEGACY_PREFIX) :].split(": ", 1)
        inbox.send(sender, entry["to"], text)
        moved += 1
    return moved


def latest_at(entries):
    return max((e.get("at", 0) for e in entries), default=0)


def start_cursor(store, slug, entries):
    store.redis.set(store.key(slug, "chat-cursor"), latest_at(entries))


def master_name(store, slug):
    return next((a.name for a in store.agents(slug) if a.lane == MASTER and a.state != "finished"), "")


def relay_operator_chat(store, slug, entries):
    """Send every operator chat entry newer than the swarm's cursor; a leading @name, @eng or @ci addresses it.

    An unaddressed entry, or one for an agent not in the swarm, goes to the master, or to everyone without one.
    """
    cursor_key = store.key(slug, "chat-cursor")
    master = master_name(store, slug)
    cursor = int(store.redis.get(cursor_key) or 0)
    fresh = [
        e for e in entries if e.get("by", OPERATOR) == OPERATOR and e.get("at", 0) > cursor and not e.get("deleted")
    ]
    for entry in sorted(fresh, key=lambda e: e["at"]):
        text, to = entry.get("text", ""), ""
        if text.startswith("@") and " " in text:
            to, text = text[1:].split(" ", 1)
        if not send(store, slug, text, sender=OPERATOR, to=to or master) and to:
            send(store, slug, f"(to {to}, who is not in the swarm) {text}", sender=OPERATOR, to=master)
        store.redis.set(cursor_key, entry["at"])
    return len(fresh)
