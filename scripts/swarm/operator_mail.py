"""Operator writes on a swarm ledger as inbox items: a task's to the agent that claimed it, a chat line to its
addressee (for @swarm and @master the lead master's seat, with an information only copy of @swarm for every other live
agent), a phase's or an unclaimed task's to the live master owning its phase, everything else to the lead master. An
addressee that is gone falls back to the lead."""

import functools

from scripts.inbox.seats import seat_address
from scripts.inbox.seen import write_ref
from scripts.swarm.store import MASTER, SwarmError
from scripts.swarm_ledger.ledger_gate import IGNORED_KINDS, MENTION_RE
from scripts.swarm_v2 import masters

OPERATOR = "operator"
SYNC_ORDER = "sync requested"
EVERYONE = "swarm"
SENT_TTL_S = 30 * 24 * 3600
MASTER_RULE = "Answer the operator in your next turn: reply to this message saying what you will do."
ANSWER_RULE = (
    "Answer the operator in your next turn: reply to this message saying what you will do. If the work is not "
    "yours, send it to {master} with agentihooks msg send naming why, and say so in your reply. "
    "Never only forward it."
)
INFO_RULE = "For your awareness only: {master} answers this line for the swarm. Do not reply to it; close it done."


def _live(agents):
    return [a for a in agents if a.state != "finished"]


def addresses(slug, event, doc, agents, owners):
    """owners is called only when an item falls back to a master, so a write with a live addressee reads no seats."""
    live = _live(agents)
    master = master_address(slug, live)
    if event.get("kind") == SYNC_ORDER:
        return [a.seat or a.name for a in live]
    target = event.get("target", "")
    found = []
    if target == "chat" or target.startswith("notes/"):
        to = mentioned(event)
        if to == EVERYONE:
            everyone = [a.seat or a.name for a in live]
            return everyone if master in everyone else [*everyone, master]
        if to == MASTER:
            return [master]
        found = [a for a in live if to in (a.name, a.lane)]
    elif target.startswith("tasks/"):
        task_id = target.split("/")[1]
        claimant = next((t.get("claimed_by") for t in doc.get("tasks", []) if t.get("id") == task_id), "")
        found = [a for a in live if claimant and a.name == claimant]
    return [a.seat or a.name for a in found] or [masters.route(target, doc, owners(), masters.live_seats(live), master)]


def mentioned(event):
    mention = MENTION_RE.match(event.get("note_text", event.get("text", "")))
    return mention.group(1) if mention else ""


def informed(event, address, master):
    """A line to the whole swarm is the master's to answer; every other agent gets it for awareness only."""
    return address != master and mentioned(event) == EVERYONE


def master_address(slug, live):
    """The lead answers the operator; a lone live master in another seat answers while the lead seat is empty."""
    lead = seat_address(slug, MASTER)
    seated = masters.live_seats(live)
    return seated[0] if len(seated) == 1 else lead


def primed(text, event, address, master):
    """An operator chat line carries the rule that its receiver answers it, so no agent only forwards it."""
    if event.get("target") != "chat":
        return text
    if informed(event, address, master):
        return f"{text}\n{INFO_RULE.format(master=master)}"
    rule = MASTER_RULE if address == master else ANSWER_RULE.format(master=master)
    return f"{text}\n{rule}"


def relay(inbox, store, slug, doc, events, line):
    """Send each new operator write once; a write already relayed is skipped. No swarm on the ledger sends nothing."""
    try:
        store.config(slug)
    except SwarmError:
        return []
    agents, sent = store.agents(slug), []
    owners = functools.cache(lambda: masters.MasterSeats(inbox.redis).owners(slug, doc))
    for event in events:
        if event.get("by") != OPERATOR or event.get("kind") in IGNORED_KINDS:
            continue
        ref = write_ref(slug, event)
        if not inbox.redis.set(inbox.key("ledger-sent", ref), 1, nx=True, ex=SENT_TTL_S):
            continue
        text = f"On ledger {slug}: {line(event)}"
        master = master_address(slug, _live(agents))
        sent += [
            inbox.send(
                OPERATOR,
                address,
                primed(text, event, address, master),
                ref=ref,
                fyi=informed(event, address, master),
            )
            for address in addresses(slug, event, doc, agents, owners)
        ]
    return sent
