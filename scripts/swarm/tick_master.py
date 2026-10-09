"""The master pass after an outage: once a swarm has had no live master for the down window, force a master launch;
when that launch fails, promote one live engineer whose only purpose is restoring the master, until one binds."""

import json
import os

from scripts.inbox.seats import seat_address
from scripts.inbox.store import InboxStore
from scripts.swarm import master_alarm, master_start
from scripts.swarm.store import MASTER

KEY = "master-outage"
SENDER = "swarm"
FORCED = "master down {minutes} minutes, forced a master launch"
NO_HOOK = "the forced master launch reported no hook within two minutes"
NO_LAUNCH = "no master launched"
PROMPT = (
    "PROMOTED: swarm {slug} has had no live master for {minutes} minutes and the forced master launch failed: "
    "{reason}. The exact launch error: {error}. This replaces any master down notice you were sent. You stay {name} "
    "on task {task}, but until a master binds your only purpose is, in this order: "
    "1. Bring the master back as soon as possible. The tick forces a new master launch every {minutes} minutes; read "
    "why it fails with agentihooks swarm {slug} status and journalctl --user -u agentihooks-swarm.service. "
    "2. Fix the causes of the outage right away in code: find where that error is raised and fix it through your own "
    "pull request into dev, never as follow ups. Once it merges and wt.sh done syncs local dev, the next forced "
    "launch runs the fixed code, or start one at once with agentihooks swarm {slug} master up --new. "
    "3. Throughout, tell the swarm and the operator what failed and what you are doing, with agentihooks swarm "
    '{slug} say "<text>" and agentihooks swarm {slug} say --to eng "<text>". '
    "You do not act as master: never answer chat as master, write tasks or steer the swarm. "
    "The tick ends this promotion when a real master binds and tells you; then return to task {task}."
)
HAND_BACK = "The master {master} is live again, so your promotion has ended. Return to your task {task}."
STOPPED = "The swarm is stopping, so your promotion has ended. Return to your task {task}."
PROMOTED_NOTICE = (
    "The master has been down {minutes} minutes and a forced launch failed. One engineer is promoted to restore it "
    "and will report what failed and what it is doing."
)
HANDED_BACK_NOTICE = "A master is live again. The promoted engineer is back on its own task."
STOPPING = "the swarm is stopping"
STOPPED_NOTICE = "The swarm is stopping, so the promoted engineer is back on its own task."
NOBODY_NOTICE = (
    "The master has been down {minutes} minutes, a forced launch failed and no live engineer can be promoted to "
    "restore it. Operator action is required."
)


def down_minutes(environ) -> float:
    return float(environ.get("AGENTIHOOKS_MASTER_DOWN_MINUTES", "5"))


def read(store, slug) -> dict:
    raw = store.redis.get(store.key(slug, KEY))
    return json.loads(raw) if raw else {}


def promoted(store, slug) -> str:
    return read(store, slug).get("promoted", "")


def prompt(slug, agent, reason, minutes, error) -> str:
    return PROMPT.format(
        slug=slug, minutes=_shown(minutes), reason=reason, error=error, name=agent.name, task=agent.task
    )


def status_line(state) -> str:
    if not state.get("promoted"):
        return ""
    return f"promoted  {state['promoted']}  restoring the master: {state['failure']}"


def run(slug, config, store, ledger, runtime, now_ms, launch) -> list[str]:
    """launch runs the tick's own master launch and returns its actions."""
    state, minutes = read(store, slug), down_minutes(os.environ)
    forcing = bool(state) and config.state != "stopping" and _due(state, now_ms, minutes)
    if forcing and not _starting(store, slug):
        store.redis.delete(store.key(slug, "master-start"))
    launched = launch()
    actions = [FORCED.format(minutes=_shown(minutes)), *launched] if forcing else launched
    if config.state == "stopping":
        master_alarm.clear(store, slug, back=False)
        return actions + _stop(slug, store, ledger, state, now_ms)
    if master := _bound(store, slug, runtime):
        back = master_alarm.clear(store, slug)
        return actions + _hand_back(slug, store, ledger, state, master.name, now_ms) + back
    if not state:
        _save(store, slug, {"since": now_ms})
        return actions
    if forcing:
        state = {**state, "forced_at": now_ms}
        if not _starting(store, slug):
            state["failure"] = "; ".join(launched) or NO_LAUNCH
    elif _starting(store, slug) and now_ms - state.get("forced_at", now_ms) >= master_start.DEADLINE_MS:
        state["failure"] = NO_HOOK
    if state.get("failure"):
        state, promoted_actions = _promote(slug, store, ledger, runtime, state, minutes, now_ms)
        actions += promoted_actions
    _save(store, slug, state)
    return actions


def _due(state, now_ms, minutes):
    return now_ms - state.get("forced_at", state["since"]) >= minutes * 60 * 1000


def _shown(minutes):
    return f"{minutes:g}"


def _save(store, slug, state):
    store.redis.set(store.key(slug, KEY), json.dumps(state))


def _masters(store, slug):
    return [a for a in store.agents(slug) if a.lane == MASTER and a.state != "finished"]


def _starting(store, slug):
    return bool(_masters(store, slug))


def _bound(store, slug, runtime):
    live = runtime.live_names()
    return next((a for a in _masters(store, slug) if a.state == "working" and a.name in live), None)


def _engineer(store, slug, name):
    return next((a for a in store.agents(slug) if a.name == name), None)


def _promote(slug, store, ledger, runtime, state, minutes, now_ms):
    live, actions = runtime.live_names(), []
    holder = state.get("promoted")
    if holder in live and _engineer(store, slug, holder):
        return state, []
    if holder:
        if gone := _engineer(store, slug, holder):
            store.seats.note(gone.seat, "promotion lost", "the promoted engineer is gone", now_ms)
        store.seats.note(seat_address(slug, MASTER), "promotion lost", holder, now_ms)
        actions.append(f"promoted {holder} is gone")
        state = {k: v for k, v in state.items() if k != "promoted"}
    engineers = sorted(
        (
            a
            for a in store.agents(slug)
            if a.lane == "eng" and a.state not in ("finished", "awaiting-decision") and a.name in live
        ),
        key=lambda a: (a.started_at, a.name),
    )
    if not engineers:
        if not state.get("nobody_told"):
            state = {**state, "nobody_told": True}
            _save(store, slug, state)
            ledger.notify(slug, NOBODY_NOTICE.format(minutes=_shown(minutes)))
        return state, actions + ["no live engineer to promote"]
    agent, reason = engineers[0], state["failure"]
    error = master_alarm.error(store, slug, state.get("forced_at", state["since"])) or reason
    InboxStore(store.redis).send(SENDER, agent.name, prompt(slug, agent, reason, minutes, error))
    store.seats.note(agent.seat, "promoted", reason, now_ms)
    store.seats.note(agent.seat, "message", "the promoted prompt", now_ms)
    store.seats.note(seat_address(slug, MASTER), "promoted", f"{agent.name}: {reason}", now_ms)
    state = {**state, "promoted": agent.name}
    _save(store, slug, state)
    ledger.notify(slug, PROMOTED_NOTICE.format(minutes=_shown(minutes)))
    return state, actions + [
        f"promoted {agent.name} to restore the master: {reason}",
        f"sent {agent.name} the promoted prompt",
    ]


def _ending(slug, store, state):
    store.redis.delete(store.key(slug, KEY))
    return _engineer(store, slug, state.get("promoted"))


def _hand_back(slug, store, ledger, state, master, now_ms):
    agent = _ending(slug, store, state)
    if not agent:
        return []
    ledger.notify(slug, HANDED_BACK_NOTICE)
    text = HAND_BACK.format(master=master, task=agent.task)
    return _close(store, slug, agent, text, master, f"master {master} bound", now_ms)


def _stop(slug, store, ledger, state, now_ms):
    agent = _ending(slug, store, state)
    if not agent:
        return []
    ledger.notify(slug, STOPPED_NOTICE)
    return _close(store, slug, agent, STOPPED.format(task=agent.task), STOPPING, STOPPING, now_ms)


def _close(store, slug, agent, text, why, ended, now_ms):
    InboxStore(store.redis).send(SENDER, agent.name, text)
    store.seats.note(agent.seat, "handed back", why, now_ms)
    store.seats.note(agent.seat, "message", "the hand back", now_ms)
    store.seats.note(seat_address(slug, MASTER), "handed back", f"{agent.name}: {why}", now_ms)
    return [f"{ended}, ended the promotion of {agent.name}", f"sent {agent.name} the hand back"]
