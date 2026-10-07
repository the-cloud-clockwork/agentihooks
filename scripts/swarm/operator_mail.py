"""Operator writes on a swarm ledger as inbox items: a task's to the agent that claimed it, a chat line to its
addressee (every live agent for @swarm), everything else to the master's seat. An addressee that is gone falls back to the master."""

from scripts.inbox.seats import seat_address
from scripts.inbox.seen import write_ref
from scripts.swarm.store import MASTER, SwarmError
from scripts.swarm_ledger.ledger_gate import IGNORED_KINDS, MENTION_RE

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


def _live(agents):
    return [a for a in agents if a.state != "finished"]


def addresses(slug, event, doc, agents):
    live = _live(agents)
    master = master_address(slug, live)
    if event.get("kind") == SYNC_ORDER:
        return [a.seat or a.name for a in live]
    target = event.get("target", "")
    found = []
    if target == "chat" or target.startswith("notes/"):
        mention = MENTION_RE.match(event.get("note_text", event.get("text", "")))
        to = mention.group(1) if mention else ""
        if to == EVERYONE:
            everyone = [a.seat or a.name for a in live]
            return everyone if master in everyone else [*everyone, master]
        found = [a for a in live if to in (a.name, a.lane)]
    elif target.startswith("tasks/"):
        task_id = target.split("/")[1]
        claimant = next((t.get("claimed_by") for t in doc.get("tasks", []) if t.get("id") == task_id), "")
        found = [a for a in live if claimant and a.name == claimant]
    return [a.seat or a.name for a in found] or [master]


def master_address(slug, live):
    boss = next((a for a in live if a.lane == MASTER), None)
    return (boss.seat or boss.name) if boss else seat_address(slug, MASTER)


def primed(text, event, address, master):
    """An operator chat line carries the rule that its receiver answers it, so no agent only forwards it."""
    if event.get("target") != "chat":
        return text
    rule = MASTER_RULE if address == master else ANSWER_RULE.format(master=master)
    return f"{text}\n{rule}"


def relay(inbox, store, slug, doc, events, line):
    """Send each new operator write once; a write already relayed is skipped. No swarm on the ledger sends nothing."""
    try:
        store.config(slug)
    except SwarmError:
        return []
    agents, sent = store.agents(slug), []
    for event in events:
        if event.get("by") != OPERATOR or event.get("kind") in IGNORED_KINDS:
            continue
        ref = write_ref(slug, event)
        if not inbox.redis.set(inbox.key("ledger-sent", ref), 1, nx=True, ex=SENT_TTL_S):
            continue
        text = f"On ledger {slug}: {line(event)}"
        master = master_address(slug, _live(agents))
        sent += [
            inbox.send(OPERATOR, address, primed(text, event, address, master), ref=ref)
            for address in addresses(slug, event, doc, agents)
        ]
    return sent
