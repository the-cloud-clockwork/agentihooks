"""Bring a swarm master online from the operator's terminal: reopen the last master's own conversation or start a new one."""

import json
from dataclasses import dataclass, fields, replace
from datetime import datetime, timezone

from scripts.handoff import transfers
from scripts.inbox.seats import seat_address
from scripts.swarm import affinity, effort_range, live_binding, model_pick
from scripts.swarm.resume import blocker
from scripts.swarm.snapshot import ledger_path
from scripts.swarm.store import MASTER, AgentRecord, SwarmError
from scripts.swarm.templates import DEFAULT_PROFILES
from scripts.swarm.tick import _placed, primed

LAST, NEW = "last", "new"
ANSWERS = {"1": LAST, LAST: LAST, "2": NEW, NEW: NEW}
QUESTION = (
    "  1) bring back the last master with its own conversation\n"
    "  2) start a new master that reads the seat handoff, recap and learned notes"
)
CHOOSE = "Choose 1 or 2: "
OFFER = "Start a new master instead? [y/N] "
NONE_RECORDED = "no earlier master is recorded"
RESUMED = (
    "The operator brought you back with agentihooks swarm {slug} master up as {name}, the master of this swarm. "
    "Time passed and other agents may have moved the work. Before acting, re-read the ledger {ledger} and your "
    "inbox, then continue as master."
)
RECORD_FIELDS = {f.name for f in fields(AgentRecord)}


@dataclass(frozen=True)
class Launched:
    master: str
    pane: str
    seat: str
    choice: str


@dataclass(frozen=True)
class Previous:
    agent: AgentRecord
    ran_at: int


def last_master(store, slug, live):
    """The newest master of the swarm that is not running now, from the agent registry or the swarm history."""
    return getattr(_previous(store, slug, live), "agent", None)


def _previous(store, slug, live):
    current = [Previous(a, a.started_at) for a in store.agents(slug) if a.lane == MASTER]
    names = {p.agent.name for p in current}
    history = [json.loads(row) for row in store.redis.lrange(store.key(slug, "history"), 0, -1)]
    ended = [
        Previous(AgentRecord(**{k: v for k, v in row.items() if k in RECORD_FIELDS}), row.get("ended_at") or 0)
        for row in history
        if row.get("lane") == MASTER and row["name"] not in names
    ]
    found = [p for p in current + ended if p.agent.name not in live]
    return max(found, key=lambda p: p.ran_at, default=None)


def describe(previous):
    if previous is None:
        return "No earlier master is recorded for this swarm."
    at = datetime.fromtimestamp(previous.ran_at / 1000, timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    return f"Last master: {previous.agent.name} on {previous.agent.harness or 'an unknown harness'}, last ran {at}"


def _answer(ask, question):
    try:
        return ask(question).strip().lower()
    except EOFError as exc:
        raise SwarmError("no answer on standard input; pass --last or --new") from exc


def _choose(ask):
    while True:
        found = ANSWERS.get(_answer(ask, CHOOSE))
        if found:
            return found


def _profile(config):
    return config.lanes.get(MASTER, {}).get("profile") or DEFAULT_PROFILES[MASTER]


def _why(previous, config):
    if previous is None:
        return NONE_RECORDED
    if not previous.agent.harness:
        return "its harness is not recorded"
    return blocker(previous.agent, config.repo)


def _resume(store, slug, runtime, at, previous):
    config = store.ensure_code(slug)
    seat = seat_address(slug, MASTER)
    agent = replace(
        previous.agent, lane=MASTER, task=MASTER, seat=seat, profile=previous.agent.profile or _profile(config)
    )
    text = RESUMED.format(slug=slug, name=agent.name, ledger=ledger_path(slug))
    placed = runtime.resume(config, agent, text)
    store.seats.occupy(seat, agent.name, at)
    record = replace(
        _placed(agent, placed),
        harness=placed.harness or agent.harness,
        account=placed.account or agent.account,
        profile=placed.profile or agent.profile,
        model=placed.model or agent.model,
        effort=placed.effort or agent.effort,
        started_at=at,
        idle_ticks=0,
    )
    store.put_agent(slug, record)
    return Launched(record.name, record.pane_id, seat, LAST)


def fill(saved, config):
    """A saved launch with its empty keys taken from the swarm config: master profile, affinity harness, frontier model."""
    harness = saved.get("harness") or affinity.desired(config) or "claude"
    pick = model_pick.frontier(harness)
    defaults = {
        "profile": _profile(config),
        "harness": harness,
        "model": pick.model,
        "effort": effort_range.clamp(harness, pick.effort, effort_range.of(config)),
    }
    return {**defaults, **{key: value for key, value in saved.items() if value}}


def _filled(task, config):
    saved = task.get("launch_assignment")
    if saved is not None and not live_binding.complete(saved):
        task = {**task, "launch_assignment": fill(saved, config)}
    envelope = task.get("handoff_envelope") or {}
    launch = envelope.get("launch")
    if (task.get("handoff") or launch) and not live_binding.complete(launch):
        task = {**task, "handoff_envelope": {**envelope, "launch": fill(launch or {}, config)}}
    return task


def _new(store, slug, runtime, at):
    config = store.ensure_code(slug)
    name = store.next_name(slug, MASTER, at)
    record = AgentRecord(name, MASTER, MASTER, started_at=at, state="starting", seat=seat_address(slug, MASTER))
    store.put_agent(slug, record)
    try:
        store.seats.occupy(record.seat, name, at)
        transfer = transfers.attach(store, slug, record)
        task = {"id": MASTER, "handoff": store.handoff(slug, MASTER), "peer": store.peer(slug), "transfer": transfer}
        task = _filled(primed(store, slug, record.seat, task), config)
        affinity.handed_off(store, slug)
        placed = runtime.spawn(config, MASTER, name, task)
    except Exception as exc:
        transfers.failed(store, slug, record)
        store.drop_agent(slug, name)
        affinity.failed(store, slug, str(exc))
        raise SwarmError(f"the new master could not start: {exc}") from exc
    affinity.placed(store, slug, placed.harness)
    record = replace(_placed(record, placed), state="working")
    store.put_agent(slug, record)
    store.clear_handoff(slug, MASTER)
    store.redis.hdel(store.key(slug, "launch-assignments"), MASTER)
    return Launched(name, record.pane_id, record.seat, NEW)


def up(store, slug, runtime, at, choice, ask, say):
    """Launch the chosen master; ask last or new when no choice is given. Never refuses because a master is live."""
    previous = _previous(store, slug, runtime.live_names())
    asked = not choice
    if asked:
        say(describe(previous))
        say(QUESTION)
        choice = _choose(ask)
    if choice == NEW:
        return _new(store, slug, runtime, at)
    why = _why(previous, store.config(slug))
    if not why:
        try:
            return _resume(store, slug, runtime, at, previous)
        except Exception as exc:
            why = f"resume did not start: {exc}"
    if not asked:
        raise SwarmError(f"the last master cannot be resumed: {why}; run master up --new for a new master")
    say(f"The last master cannot be resumed: {why}")
    if _answer(ask, OFFER) not in ("y", "yes"):
        raise SwarmError("no master started")
    return _new(store, slug, runtime, at)
