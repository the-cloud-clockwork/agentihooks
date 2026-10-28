"""Swarm chat over the inbox: addressed swarm chat becomes inbox items.

The inbox hooks deliver an item at the receiver's next tool call and the tick's wake pass prompts idle panes.
Replies addressed to the operator are posted as a comment on the ledger entry his item sat on, else on the page chat.
"""

import contextlib
import functools
import json

from scripts.inbox import links, outbox
from scripts.inbox.store import Contended, DuplicateItem, InboxError, InboxStore, Item
from scripts.swarm.ledger_client import LedgerUnreachable
from scripts.swarm.store import RedisStore, SwarmError
from scripts.swarm_v2 import master_fence

READY = ("idle", "done")
OPERATOR = "operator"
BY = "swarm"
LEGACY_PREFIX = "[swarm chat] "
MARK = "[swarm delivery]"
REFUSED = (
    "The ledger page refused to show your message {id} to the operator: {reason}. "
    "Rewrite it in plain words and send it again."
)
UNSHOWN = (
    "Your message {id} to the operator has not reached the ledger page: {reason}. "
    "It stays queued and the swarm posts it again on every tick until the page accepts it."
)


class HerdrMessenger:
    def __init__(self, herdr=None):
        from scripts.swarm import runtime

        self.herdr = herdr or runtime.herdr_call
        self.target = runtime.pane_target

    def agent_status(self, agent):
        result = self.herdr(["agent", "get", self.target(agent)])
        found = result.get("agent", result)
        state = found.get("agent_status") or found.get("status") or "unknown"
        if agent.harness == "codex" and state == "blocked":
            from scripts.swarm.pane import content_refusal, refusal_input

            capture = self.herdr(["pane", "read", agent.pane_id, "--source", "visible", "--format", "ansi"])
            if content_refusal(agent.harness, capture["text"]) and not refusal_input(capture["text"]):
                return "idle"
        return state

    def prompt(self, agent, text):
        self.herdr(["agent", "prompt", self.target(agent), marked(text)])

    def typed_input(self, agent):
        from scripts.swarm.pane import content_refusal, refusal_input, typed_input

        capture = self.herdr(["pane", "read", agent.pane_id, "--source", "visible", "--format", "ansi"])
        if content_refusal(agent.harness, capture["text"]):
            return refusal_input(capture["text"])
        return typed_input(capture["text"])


def marked(text):
    """A prompt the swarm types into a pane, marked so the hooks never count it as the operator's."""
    return f"{MARK} {text}"


def envelope(item: Item, delivery_id: str, recipient: str) -> str:
    metadata = {
        "item_id": item.id,
        "delivery_id": delivery_id,
        "sender": item.sender,
        "recipient": recipient,
        "task": item.task,
        "kind": "fyi" if item.fyi else "message",
    }
    return f"{MARK}\n{json.dumps(metadata)}\n\n{item.text}"


def recipients(store, slug, to, sender):
    to, sender = store.names.resolve(to), store.names.resolve(sender)
    agents = [a for a in store.agents(slug) if a.name != sender and a.state != "finished"]
    if to in ("", "all"):
        return agents
    found = [a for a in agents if to in (a.lane, a.name, a.seat)]
    if not found:
        raise SwarmError(f"nobody in swarm {slug} answers to {to}")
    return found


def send(store, slug, text, sender, to, fyi=False):
    found = recipients(store, slug, to, sender)
    inbox = InboxStore(store.redis)
    for agent in found:
        links.check_send(inbox, sender, agent.name)
        master_fence.check_send(inbox, sender, agent.name)
    for agent in found:
        outbox.post(inbox, outbox.Post(sender, agent.name, text, fyi=fyi, task=agent.task))
    return [a.name for a in found]


def queue(slug, text, sender, to, fyi=False):
    """A swarm say while Redis is unreachable: its recipients are looked up when the outbox replays."""
    entry = outbox.record("say", sender, {"slug": slug, "to": to, "text": text, "fyi": fyi})
    return outbox.submit(None, entry, None)


def apply_say(inbox, sender, entry):
    args, store = entry["args"], RedisStore(inbox.redis)
    try:
        slug = store.names.swarm_slug(args["slug"])
        found = recipients(store, slug, args["to"], sender)
    except SwarmError as exc:
        raise InboxError(str(exc)) from exc
    for agent in found:
        links.check_send(inbox, sender, agent.name)
        master_fence.check_send(inbox, sender, agent.name)
    for agent in found:
        item = outbox.stable_id(entry["item"], agent.name)
        with contextlib.suppress(DuplicateItem):
            inbox.send(sender, agent.name, args["text"], fyi=args["fyi"], task=agent.task, item_id=item)
    return [a.name for a in found]


def relay_to_page(inbox, slug, agents, ledger):
    names = {a.name for a in agents}
    shown = 0
    for item in inbox.inbox(OPERATOR):
        sender = inbox.names.resolve(item.sender)
        if item.state != "pending" or (sender not in names and inbox.names.slug_of(sender) != slug):
            continue
        write = functools.partial(
            show, ledger, slug, item, sender, inbox.entry_of(item.id), operator_line(inbox, slug, item)
        )
        try:
            posted = post(inbox, item, write)
        except LedgerUnreachable as exc:
            unshown(inbox, item, exc)
            break
        if not posted:
            continue
        inbox.close(item.id, OPERATOR, "done", "shown on the ledger page")
        shown += 1
    return shown


def show(ledger, slug, item, sender, entry, line):
    if entry:
        return ledger.relay_comment(slug, entry, item.text, sender, item.id)
    return ledger.relay(slug, item.text, sender, item.id, line)


def operator_line(inbox, slug, item):
    if not item.ref:
        return ""
    try:
        asked = inbox.get(item.ref)
    except InboxError:
        return ""
    ledger, _, rest = asked.ref.partition(":")
    if asked.sender != OPERATOR or ledger != slug:
        return ""
    return rest.partition(":")[2]


def post(inbox, item, write):
    """Run the ledger write that shows an inbox item; a refusal closes only that item and tells its sender why."""
    try:
        refused = "the swarm notice was dropped" if write() is False else ""
    except LedgerUnreachable:
        raise
    except SwarmError as exc:
        refused = exc
    if refused:
        if not may_cancel(inbox, item, refused):
            return False
        inbox.close(item.id, item.address, "cancel", f"refused by the ledger page: {refused}")
        if item.sender != BY:
            inbox.send(BY, item.sender, REFUSED.format(id=item.id, reason=refused))
        return False
    return True


def unshown(inbox, item, reason):
    """The page did not answer: the item stays pending for the next pass and its sender's master hears it once."""
    door, _ = inbox.front_door_of(item.sender)
    with contextlib.suppress(DuplicateItem):
        inbox.send(
            BY,
            door or item.sender,
            UNSHOWN.format(id=item.id, reason=reason),
            fyi=True,
            item_id=outbox.stable_id("unshown", item.id),
        )


def may_cancel(inbox, item, refused):
    """False while the entry item a refused answer closed cannot be reopened yet: the answer waits for the next pass."""
    if not (item.ref and inbox.entry_of(item.id)):
        return True
    try:
        inbox.reopen(item, BY, f"reopened: the answer was refused by the ledger page: {refused}")
    except Contended:
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
