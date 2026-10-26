"""Operator writes on a swarm ledger as inbox items: a task's to the agent that claimed it, a chat line to its
addressee (for @swarm and @master the lead master, with an information only copy of @swarm for every other live agent),
everything else to the lead master, the front door: a backend master receives no operator write beyond a sync
order and the @swarm information copy. The lead is the lead seat while it is live, else the lowest numbered live
master. A gone addressee falls back to the lead."""

from scripts.inbox import outbox
from scripts.inbox.seen import write_ref
from scripts.swarm.store import MASTER, SwarmError
from scripts.swarm_ledger.ledger_gate import IGNORED_KINDS, MENTION_RE
from scripts.swarm_v2 import masters

OPERATOR = "operator"
SYNC_ORDER = "sync requested"
EVERYONE = "swarm"
SENT_TTL_S = 30 * 24 * 3600
ENTRY_LISTS = ("tasks", "phases", "questions", "followups", "notes")
PLAN_EVENT_TARGETS = ("plans/", "slices/")
MASTER_RULE = "Answer the operator in your next turn: reply to this message saying what you will do."
ANSWER_RULE = (
    "Answer the operator in your next turn: reply to this message saying what you will do. If the work is not "
    "yours, send it to {master} with agentihooks msg send naming why, and say so in your reply. "
    "Never only forward it."
)
INFO_RULE = "For your awareness only: {master} answers this line for the swarm. Do not reply to it; close it done."


def _live(agents):
    return [a for a in agents if a.state != "finished"]


def addresses(slug, event, doc, agents, held):
    live = _live(agents)
    master = masters.lead_of(slug, held)
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
        found = [a for a in live if to in (a.name, a.lane) and a.lane != MASTER]
    elif target.startswith("tasks/"):
        task_id = target.split("/")[1]
        claimant = next((t.get("claimed_by") for t in doc.get("tasks", []) if t.get("id") == task_id), "")
        found = [a for a in live if claimant and a.name == claimant]
    return [a.seat or a.name for a in found] or [master]


def mentioned(event):
    mention = MENTION_RE.match(event.get("note_text", event.get("text", "")))
    return mention.group(1) if mention else ""


def informed(event, address, master):
    """A line to the whole swarm is the master's to answer; every other agent gets it for awareness only."""
    return address != master and mentioned(event) == EVERYONE


def master_address(redis, slug, agents):
    return masters.lead_of(slug, list(masters.held(redis, slug, agents)))


def primed(text, event, address, master):
    """An operator chat line carries the rule that its receiver answers it, so no agent only forwards it."""
    if event.get("target") != "chat":
        return text
    if informed(event, address, master):
        return f"{text}\n{INFO_RULE.format(master=master)}"
    rule = MASTER_RULE if address == master else ANSWER_RULE.format(master=master)
    return f"{text}\n{rule}"


def entry(event):
    """The ledger entry an operator write sits on, where an answer to it lands; '' for chat and writes on no entry."""
    if event["kind"].startswith("note "):
        return f"notes/{event['id']}"
    head, _, rest = event["target"].partition("/")
    return f"{head}/{rest.partition('/')[0]}" if head in ENTRY_LISTS and rest else ""


def relay(inbox, store, slug, doc, events, line):
    """Send each new operator write once; a write already relayed is skipped. No swarm on the ledger sends nothing."""
    try:
        store.config(slug)
    except SwarmError:
        return []
    agents, sent = store.agents(slug), []
    held = list(masters.held(inbox.redis, slug, agents))
    for event in events:
        if event.get("by") != OPERATOR or event.get("kind") in IGNORED_KINDS:
            continue
        if event.get("kind") == "added" and event.get("target", "").startswith(PLAN_EVENT_TARGETS):
            continue
        ref = write_ref(slug, event)
        mark = inbox.key("ledger-sent", ref)
        if inbox.redis.exists(mark):
            continue
        text = f"On ledger {slug}: {line(event)}"
        master = masters.lead_of(slug, held)
        for address in addresses(slug, event, doc, agents, held):
            primer = primed(text, event, address, master)
            fyi = informed(event, address, master)
            item_id = outbox.stable_id(ref, address)
            if on := entry(event):
                inbox.about(item_id, on)
            mail = outbox.Post(OPERATOR, address, primer, ref=ref, fyi=fyi, item=item_id)
            item = outbox.post(inbox, mail)
            sent += [item] if item else []
        inbox.redis.set(mark, 1, ex=SENT_TTL_S)
    return sent
