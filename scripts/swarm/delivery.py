"""Swarm chat over the inbox: addressed swarm chat becomes inbox items.

The inbox hooks deliver an item at the receiver's next tool call and the tick's wake pass prompts idle panes.
Replies addressed to the operator are posted on the ledger page chat.
"""

import json

from scripts.inbox import links
from scripts.inbox.store import InboxStore
from scripts.swarm.store import SwarmError

READY = ("idle", "done")
OPERATOR = "operator"
BY = "swarm"
LEGACY_PREFIX = "[swarm chat] "
MARK = "[swarm delivery]"
REFUSED = (
    "The ledger page refused to show your message {id} to the operator: {reason}. "
    "Rewrite it in plain words and send it again."
)


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
        self.herdr(["agent", "prompt", self.target(agent), marked(text)])

    def typed_input(self, agent):
        from scripts.swarm.pane import typed_input

        capture = self.herdr(["pane", "read", agent.pane_id, "--source", "visible", "--format", "ansi"])
        return typed_input(capture["text"])


def marked(text):
    """A prompt the swarm types into a pane, marked so the hooks never count it as the operator's."""
    return f"{MARK} {text}"


def recipients(store, slug, to, sender):
    to, sender = store.names.resolve(to), store.names.resolve(sender)
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
        inbox.send(sender, agent.name, text, fyi=fyi, task=agent.task)
    return [a.name for a in found]


def relay_to_page(inbox, slug, agents, ledger):
    names = {a.name for a in agents}
    shown = 0
    for item in inbox.inbox(OPERATOR):
        sender = inbox.names.resolve(item.sender)
        if item.state != "pending" or sender not in names:
            continue
        if not post(inbox, item, lambda: ledger.relay(slug, item.text, sender)):
            continue
        inbox.close(item.id, OPERATOR, "done", "shown on the ledger page")
        shown += 1
    return shown


def post(inbox, item, write):
    """Run the ledger write that shows an inbox item; a refusal closes only that item and tells its sender why."""
    try:
        write()
    except SwarmError as exc:
        inbox.close(item.id, item.address, "cancel", f"refused by the ledger page: {exc}")
        if item.sender != BY:
            inbox.send(BY, item.sender, REFUSED.format(id=item.id, reason=exc))
        return False
    return True


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
