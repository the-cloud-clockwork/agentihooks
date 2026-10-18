"""A failed master launch, told once per outage: the linked Doctor master gets the exact error and every live agent
one notice to keep working without the master. A master that binds ends the outage."""

import json

from scripts.doctor.priming import TEMPLATE
from scripts.inbox.seats import seat_address
from scripts.inbox.store import InboxStore
from scripts.swarm.store import MASTER

KEY = "master-alarm"
SENDER = "swarm"
DOCTOR = (
    "The master of swarm {slug} failed to start: {error}. Its live agents are told to keep working their own tasks "
    "without it. Find the cause and fix it in code."
)
NOTICE = (
    "The master of swarm {slug} is down: its launch failed with {error}. Keep working your own task through to "
    "merge, put each question on the ledger with agentihooks ledger --slug {slug} --as {name} question add "
    '"<text>", and do not wait on master replies.'
)
BACK = "The master of swarm {slug} is live again and answers questions and replies as before."


def read(store, slug) -> dict:
    raw = store.redis.get(store.key(slug, KEY))
    return json.loads(raw) if raw else {}


def error(store, slug, since) -> str:
    """The recorded launch error, only when it was recorded at or after since."""
    state = read(store, slug)
    return state.get("error", "") if state.get("at", -1) >= since else ""


def failed(store, slug, text, at) -> None:
    _save(store, slug, {**read(store, slug), "error": text, "at": at})


def clear(store, slug) -> list[str]:
    state = drop(store, slug)
    told = [*state.get("told", []), *([seat_address(state["doctor"], MASTER)] if state.get("doctor") else [])]
    inbox = InboxStore(store.redis)
    for address in told:
        inbox.send(SENDER, address, BACK.format(slug=slug))
    return [f"told {address} the master is back" for address in told]


def drop(store, slug) -> dict:
    state = read(store, slug)
    store.redis.delete(store.key(slug, KEY))
    return state


def run(slug, store, runtime, promoted) -> list[str]:
    """promoted names the engineer restoring the master, whose promoted prompt replaces this notice."""
    state = read(store, slug)
    if not state.get("error") or store.config(slug).state == "stopping":
        return []
    told, live = state.get("told", []), runtime.live_names()
    agents = sorted(
        a.name
        for a in store.agents(slug)
        if a.lane != MASTER and a.state != "finished" and a.name in live and a.name not in told and a.name != promoted
    )
    doctor = "" if state.get("doctor") else _doctor(store, slug)
    if not (agents or doctor):
        return []
    _save(store, slug, {**state, "doctor": state.get("doctor") or doctor, "told": sorted([*told, *agents])})
    inbox, actions = InboxStore(store.redis), []
    if doctor:
        inbox.send(SENDER, seat_address(doctor, MASTER), DOCTOR.format(slug=slug, error=state["error"]))
        actions.append(f"told the Doctor {doctor} the master launch failed: {state['error']}")
    for name in agents:
        inbox.send(SENDER, name, NOTICE.format(slug=slug, error=state["error"], name=name))
        actions.append(f"told {name} the master is down")
    return actions


def _doctor(store, slug):
    peer = store.peer(slug)
    return peer if peer in store.slugs() and store.config(peer).template == TEMPLATE else ""


def _save(store, slug, state):
    store.redis.set(store.key(slug, KEY), json.dumps(state))
