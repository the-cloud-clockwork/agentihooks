"""Wake pass run by the swarm tick: prompt idle herdr panes holding pending inbox items, escalate items nobody reads.

Ladder per pending item: up to three wakes one retry window apart, then an inbox item for the swarm master,
then a follow-up on the ledger page. Every step is appended to the item's history, so the count survives restarts.
"""

from dataclasses import replace

from scripts.inbox import addresses, exits
from scripts.inbox.dispatch import Dispatcher
from scripts.inbox.seats import is_seat
from scripts.inbox.seen import SEEN_ON_LEDGER, SeenMarks
from scripts.inbox.store import redelivery_ms
from scripts.swarm import capabilities, idle, outage
from scripts.swarm.delivery import READY, post
from scripts.swarm.store import MASTER, RedisStore, SwarmError
from scripts.swarm_ledger import ledger_comments

MAX_WAKES = 3
WINDOW_ENV = "AGENTIHOOKS_INBOX_RETRY_WINDOW_S"
DEFAULT_WINDOW_S = 300
QUIET_ENV = "AGENTIHOOKS_INBOX_QUIET_S"
DEFAULT_QUIET_S = 180
IN_USE = "in use"
BY = "swarm"
WAKE_TEXT = (
    "You have unread inbox messages: run agentihooks msg inbox, then read each one and answer it with agentihooks "
    "msg reply or the swarm commands, never as text in this terminal, and close the rest."
)
WOKEN, TO_MASTER, TO_OPERATOR = "woken", "escalated_master", "escalated_operator"


def window_ms(environ):
    return int(environ.get(WINDOW_ENV) or DEFAULT_WINDOW_S) * 1000


def quiet_ms(environ):
    return int(environ.get(QUIET_ENV) or DEFAULT_QUIET_S) * 1000


def typed_wake(inbox, agent):
    return (
        bool(agent.pane_id)
        and agent.lane != MASTER
        and not capabilities.of_agent(agent).push_inbox
        and not Dispatcher(inbox).owner(agent.name)
    )


def decide(item, pane, history, now_ms, window, slug, since=0):
    """pane is the receiver's herdr status, None when it sits outside herdr; the window clock skips the swarm's
    outage time since the item's last step, and starts no earlier than since. since None holds the item."""
    if item.state != "pending" or item.fyi or since is None:
        return None
    steps = [e.get("event") for e in history]
    if TO_OPERATOR in steps:
        return None
    last = max(since, *(e["at"] for e in history))
    due = now_ms - last - outage.paused_ms(slug, last, now_ms) >= window
    if TO_MASTER in steps:
        return TO_OPERATOR if due else None
    wakes = steps.count(WOKEN)
    if pane in READY and wakes < MAX_WAKES and (wakes == 0 or due):
        return WOKEN
    return TO_MASTER if due and not item.ref.startswith(("inbox-escalation:", "spawn-stall:")) else None


def raised_note(master: str, raised_id: str) -> str:
    return f"raised to {master} as message {raised_id}"


def raised_id(entry: dict) -> str:
    """The master item a history entry raised, '' for any other entry."""
    return entry["reason"].rpartition(" ")[2] if entry.get("event") == TO_MASTER else ""


def wake_pass(inbox, slug, agents, herdr, ledger, now_ms, window, quiet=DEFAULT_QUIET_S * 1000):
    names, config = {a.name for a in agents}, _swarm_config(inbox, slug)
    panes = {a.name: a for a in agents if typed_wake(inbox, a)}
    boss = next((a for a in agents if a.lane == MASTER), None)
    master = (boss.seat or boss.name) if boss else ""
    marks = SeenMarks(inbox.redis)
    statuses, prompted = {}, set()
    actions = [f"redelivered message {item.id}" for item in inbox.redeliver(now_ms, redelivery_ms())]
    actions += addresses.settle_unresolved(inbox, names, _ladder(inbox, slug), now_ms, window)
    doc = None
    for item in _ladder(inbox, slug):
        item = replace(item, address=inbox.names.resolve(item.address), sender=inbox.names.resolve(item.sender))
        if item.address not in names and item.sender not in names and not item.address.endswith(f"@{slug}"):
            continue
        receiver, held = _receiver(inbox, item.address)
        if item.sender == BY and item.ref.startswith(f"{slug}:event:"):
            if doc is None:
                doc = ledger.state(slug)
            if _decided(item.ref, doc):
                inbox.close(item.id, BY, "done", "decided on the ledger")
                continue
        if item.ref and receiver and marks.seen(receiver, item.ref):
            inbox.close(item.id, receiver, "done", SEEN_ON_LEDGER)
            continue
        agent = panes.get(receiver)
        if agent and agent.name not in statuses:
            statuses[agent.name] = _pane_state(inbox.redis, slug, herdr, agent, now_ms, quiet)
        since = _seat_clock(inbox, receiver, held, names, config)
        step = decide(item, statuses.get(receiver), inbox.history(item.id), now_ms, window, slug, since)
        if step == WOKEN and _still_held(inbox, held) and _wake(herdr, agent, prompted):
            if inbox.note(item.id, WOKEN, BY, f"prompted {receiver} to read its inbox", now_ms, held):
                actions.append(f"woke {receiver} for message {item.id}")
        elif step == TO_MASTER and master and receiver != boss.name and not _sent_from(inbox, item, boss, master):
            raised = inbox.send(BY, master, _master_text(item), ref=f"inbox-escalation:{item.id}")
            inbox.note(item.id, TO_MASTER, BY, raised_note(master, raised.id), now_ms)
            actions.append(f"raised message {item.id} to {master}")
        elif step in (TO_MASTER, TO_OPERATOR):
            if not post(inbox, item, lambda: ledger.followup(slug, _operator_text(item), needs_operator=True)):
                actions.append(f"the ledger page refused message {item.id}, closed it")
                continue
            inbox.note(item.id, TO_OPERATOR, BY, "shown to the operator on the ledger page", now_ms)
            actions.append(f"raised message {item.id} to the operator")
    return actions


def wake_now(inbox, slug, agents, herdr, now_ms, window, quiet=DEFAULT_QUIET_S * 1000):
    """The wake step alone, for Codex panes the moment an item lands; Claude sessions take items through the inbox
    channel, and escalation stays with the tick's wake pass."""
    actions, names, config = [], {a.name for a in agents}, _swarm_config(inbox, slug)
    for agent in agents:
        if agent.harness != "codex" or not typed_wake(inbox, agent) or outage.open_records(slug):
            continue
        mail = inbox.pending_mail(agent.name)
        if not mail:
            continue
        state = _pane_state(inbox.redis, slug, herdr, agent, now_ms, quiet)
        due = [item for item in mail if _wake_due(inbox, item, state, (names, config), (now_ms, window, slug))]
        if not due or not _wake(herdr, agent, set()):
            continue
        for item in due:
            inbox.note(item.id, WOKEN, BY, f"prompted {agent.name} to read its inbox", now_ms)
            actions.append(f"woke {agent.name} for message {item.id}")
    return actions


def _sent_from(inbox, item, boss, master):
    """The master or an earlier occupant of its seat sent the item."""
    return item.sender == boss.name or inbox.seats.known_seat(item.sender) == master


def _wake_due(inbox, item, state, swarm, clock):
    now_ms, window, slug = clock
    since = _seat_clock(inbox, *_receiver(inbox, item.address), *swarm)
    return decide(item, state, inbox.history(item.id), now_ms, window, slug, since) == WOKEN


def _ladder(inbox, slug):
    """No wake, escalation or withdrawal step runs while the swarm has an open outage."""
    return [] if outage.open_records(slug) else inbox.pending()


def _decided(ref, doc):
    collection, _, item_id = ref.partition(":event:")[2].partition("/")
    row = next((r for r in doc.get(collection, []) if r.get("id") == item_id), {})
    if collection == "followups":
        return bool(row.get("done") or row.get("needs_operator"))
    return collection == "questions" and bool(
        row.get("out_of_scope") or any(a.get("text") and not a.get("deleted") for a in row.get("answers", []))
    )


def _receiver(inbox, address):
    """The session behind an address, and for a seat the (address, generation) its wake note is guarded by."""
    if not is_seat(address):
        return address, None
    held = inbox.seats.occupant(address)
    return held.occupant, (address, held.generation)


def _still_held(inbox, held):
    return held is None or inbox.seats.occupant(held[0]).generation == held[1]


def _seat_clock(inbox, receiver, held, names, config):
    """When a seat item's window may start: None while the seat waits for a live successor it can still get, else when
    its occupant bound."""
    if held is None:
        return 0
    address, generation = held
    if (receiver not in names or inbox.seats.left(receiver, address)) and _refillable(address, config):
        return None
    bound = (e["at"] for e in inbox.seats.history(address) if e["generation"] == generation and "event" not in e)
    return max(bound, default=0)


def _refillable(address, config):
    return config is None or (config.state != "stopped" and not exits.no_successor(address, config))


def _swarm_config(inbox, slug):
    try:
        return RedisStore(inbox.redis).config(slug)
    except SwarmError:
        return None


def _status(herdr, agent):
    try:
        return herdr.agent_status(agent)
    except Exception:
        return "unknown"


def _pane_state(redis, slug, herdr, agent, now_ms, quiet):
    """A ready pane counts as in use while the operator prompted it inside the quiet window or its input line holds text."""
    state = _status(herdr, agent)
    if state not in READY:
        return state
    last = idle.last_prompt(redis, slug, agent.name)
    if last is not None and now_ms - last < quiet:
        return IN_USE
    return IN_USE if _typing(herdr, agent) else state


def _typing(herdr, agent):
    try:
        return herdr.typed_input(agent)
    except Exception:
        return ""


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
    plain = f"A message from {item.sender} to {item.address} is still unread after every wake and escalation"
    quoted = f"{plain}: {_gist(item)}"
    return plain + "." if ledger_comments.problems(quoted, "item") else quoted
