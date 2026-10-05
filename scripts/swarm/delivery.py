"""Swarm chat over the inbox: addressed swarm chat becomes inbox items.

The inbox hooks deliver an item at the receiver's next tool call and the tick's wake pass prompts idle panes.
Replies addressed to the operator are posted on the ledger page chat.
"""

import json

from scripts.inbox import links
from scripts.inbox.store import InboxStore

READY = ("idle", "done")
OPERATOR = "operator"
LEGACY_PREFIX = "[swarm chat] "


class HerdrMessenger:
    def __init__(self, herdr=None):
        from scripts.swarm import runtime

        self.herdr = herdr or runtime.herdr_call
        self.target = runtime.pane_target

    def agent_status(self, agent):
        result = self.herdr(["agent", "get", self.target(agent)])
        found = result.get("agent", result)
        return found.get("agent_status") or found.get("status") or "unknown"

    def prompt(self, agent, text):
        self.herdr(["agent", "prompt", self.target(agent), text])


def recipients(store, slug, to, sender):
    agents = [a for a in store.agents(slug) if a.name != sender and a.state != "finished"]
    if to in ("", "all"):
        return agents
    return [a for a in agents if to in (a.lane, a.name)]


def send(store, slug, text, sender, to, fyi=False):
    found = recipients(store, slug, to, sender)
    inbox = InboxStore(store.redis)
    for agent in found:
        links.check_send(inbox, sender, agent.name)
    for agent in found:
        inbox.send(sender, agent.name, text, fyi=fyi)
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
    while (raw := store.redis.lindex(outbox, 0)) is not None:
        entry = json.loads(raw)
        sender, text = "swarm", entry["text"]
        if text.startswith(LEGACY_PREFIX) and ": " in text:
            sender, text = text[len(LEGACY_PREFIX) :].split(": ", 1)
        inbox.send(sender, entry["to"], text)
        store.redis.lpop(outbox)
        moved += 1
    return moved
