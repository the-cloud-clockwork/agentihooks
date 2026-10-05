"""Wake pass run by the swarm tick: prompt idle herdr panes holding pending inbox items, escalate items nobody reads.

Ladder per pending item: up to three wakes one retry window apart, then an inbox item for the swarm master,
then a follow-up on the ledger page. Every step is appended to the item's history, so the count survives restarts.
"""

from scripts.swarm.delivery import READY
from scripts.swarm.store import MASTER

MAX_WAKES = 3
WINDOW_ENV = "AGENTIHOOKS_INBOX_RETRY_WINDOW_S"
DEFAULT_WINDOW_S = 300
BY = "swarm"
WAKE_TEXT = "You have unread inbox messages: run agentihooks msg inbox, then read and close each one."
WOKEN, TO_MASTER, TO_OPERATOR = "woken", "escalated_master", "escalated_operator"


def window_ms(environ):
    return int(environ.get(WINDOW_ENV) or DEFAULT_WINDOW_S) * 1000


def decide(item, pane, history, now_ms, window):
    """pane is the receiver's herdr status, None when it sits outside herdr."""
    if item.state != "pending":
        return None
    steps = [e.get("event") for e in history]
    if TO_OPERATOR in steps:
        return None
    due = now_ms - max(e["at"] for e in history) >= window
    if TO_MASTER in steps:
        return TO_OPERATOR if due else None
    wakes = steps.count(WOKEN)
    if pane is None or wakes >= MAX_WAKES:
        return TO_MASTER if due else None
    if pane in READY and (wakes == 0 or due):
        return WOKEN
    return None


def wake_pass(inbox, slug, agents, herdr, ledger, now_ms, window):
    names = {a.name for a in agents}
    panes = {a.name: a for a in agents if a.pane_id}
    master = next((a.name for a in agents if a.lane == MASTER), "")
    statuses, prompted, actions = {}, set(), []
    for item in inbox.pending():
        if item.address not in names and item.sender not in names:
            continue
        agent = panes.get(item.address)
        if agent and agent.name not in statuses:
            statuses[agent.name] = _status(herdr, agent)
        step = decide(item, statuses.get(item.address), inbox.history(item.id), now_ms, window)
        if step == WOKEN and _wake(herdr, agent, prompted):
            inbox.note(item.id, WOKEN, BY, f"prompted {item.address} to read its inbox", now_ms)
            actions.append(f"woke {item.address} for message {item.id}")
        elif step == TO_MASTER and master and master != item.address:
            raised = inbox.send(BY, master, _master_text(item))
            inbox.note(item.id, TO_MASTER, BY, f"raised to {master} as message {raised.id}", now_ms)
            actions.append(f"raised message {item.id} to {master}")
        elif step in (TO_MASTER, TO_OPERATOR):
            ledger.followup(slug, _operator_text(item))
            inbox.note(item.id, TO_OPERATOR, BY, "shown to the operator on the ledger page", now_ms)
            actions.append(f"raised message {item.id} to the operator")
    return actions


def _status(herdr, agent):
    try:
        return herdr.agent_status(agent)
    except Exception:
        return "unknown"


def _wake(herdr, agent, prompted):
    if agent.name in prompted:
        return True
    try:
        herdr.prompt(agent, WAKE_TEXT)
    except Exception:
        return False
    prompted.add(agent.name)
    return True


def _gist(item):
    first = item.text.splitlines()[0] if item.text else ""
    return first[:200]


def _master_text(item):
    return (
        f"Nobody has read message {item.id} from {item.sender} to {item.address}: {_gist(item)}. "
        f"Check on {item.address} or pass the work on, then close this one."
    )


def _operator_text(item):
    return (
        f"A message from {item.sender} to {item.address} is still unread after every wake and escalation: {_gist(item)}"
    )
