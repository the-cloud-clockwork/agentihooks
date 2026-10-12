"""Master affinity: the harness the operator wants the master on, and the one handoff order that moves it there."""

import json

from scripts import agent_choice
from scripts.inbox.store import CLOSED, InboxStore
from scripts.swarm.store import MASTER
from scripts.swarm_v2 import masters

ORDER = (
    "The operator set the master affinity to {to}; you run on {current}. Finish the step you are on, then hand off "
    "with the handoff skill: Handoff v2 carrying every pending decision and its asker, the messages you hold, what "
    "each claimed task needs, your audit plan and every operator override still in force. Run agentihooks swarm "
    "{slug} handoff <doc> --reason operator and stop. The runtime ends this session before it starts your successor "
    "on {to} in the master seat."
)


def desired(config):
    agent = config.lanes.get(MASTER, {}).get("agent")
    return agent if agent in agent_choice.AGENTS else ""


def _key(store, slug):
    return store.key(slug, "master-affinity")


def pending(store, slug):
    raw = store.redis.get(_key(store, slug))
    return json.loads(raw) if raw else None


def live_master(store, slug):
    return next((a for a in store.agents(slug) if masters.is_lead(slug, a) and a.state != "finished"), None)


def _withdraw(store, slug):
    found = pending(store, slug)
    if found and found["state"] == "ordered":
        InboxStore(store.redis).close(found["item"], "operator", "cancel", "the master affinity was set back")
    store.redis.delete(_key(store, slug))


def order(store, slug, now_ms):
    """Send the live master one handoff order when the desired harness differs from its own; None when none is due."""
    want, master = desired(store.config(slug)), live_master(store, slug)
    if master is None:
        return pending(store, slug)
    if not want or master.harness == want:
        _withdraw(store, slug)
        return None
    found = pending(store, slug)
    if found and (found["to"], found["master"], found["state"]) == (want, master.name, "ordered"):
        return found
    _withdraw(store, slug)
    text = ORDER.format(to=want, current=master.harness or "an unknown harness", slug=slug)
    item = InboxStore(store.redis).send("operator", master.seat or master.name, text)
    found = {"to": want, "from": master.harness, "master": master.name, "item": item.id, "at": now_ms}
    found.update(state="ordered", reason="")
    store.redis.set(_key(store, slug), json.dumps(found))
    return found


def failed(store, slug, reason):
    found = pending(store, slug)
    if found:
        store.redis.set(_key(store, slug), json.dumps({**found, "state": "failed", "reason": reason}))


def handed_off(store, slug):
    """Close the order once its master left, so the successor never receives a stale handoff order."""
    found, inbox = pending(store, slug), InboxStore(store.redis)
    item = inbox.get(found["item"]) if found else None
    if item and item.state not in CLOSED:
        inbox.close(item.id, item.address, "done", "the master handed off its seat")


def placed(store, slug, harness):
    found = pending(store, slug)
    if found is None:
        return
    if harness == found["to"]:
        store.redis.delete(_key(store, slug))
    else:
        failed(store, slug, f"the successor started on {harness or 'an unknown harness'}, not {found['to']}")


def report(store, slug, config, agents):
    live = next((a for a in agents if masters.is_lead(slug, a) and a.state != "finished"), None)
    return {"desired": desired(config) or "auto", "live": live.harness if live else "", "order": pending(store, slug)}
