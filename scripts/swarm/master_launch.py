"""Bring a swarm master online from the operator's terminal: reopen the last master's own conversation or start a new one."""

import json
import os
from dataclasses import dataclass, fields, replace
from datetime import datetime, timezone

from scripts import agent_choice
from scripts.handoff import transfers
from scripts.inbox.seats import seat_address
from scripts.swarm import affinity, effort_range, launch_check, master_start, model_pick
from scripts.swarm.resume import blocker
from scripts.swarm.snapshot import ledger_source
from scripts.swarm.store import MASTER, AgentRecord, SwarmError
from scripts.swarm.templates import DEFAULT_PROFILES
from scripts.swarm.tick import placed_record, primed

LAST, NEW = "last", "new"
ANSWERS = {"1": LAST, LAST: LAST, "2": NEW, NEW: NEW}
QUESTION = (
    "  1) bring back the last master with its own conversation\n"
    "  2) start a new master that reads the seat handoff, recap and learned notes"
)
CHOOSE = "Choose 1 or 2: "
ASKS = 3
OFFER = "Start a new master instead? [y/N] "
NONE_RECORDED = "no earlier master is recorded"
RESUMED = (
    "The operator brought you back with agentihooks swarm {slug} master up as {name}, the master of this swarm. "
    "Time passed and other agents may have moved the work. Before acting, re-read the ledger with {ledger} and your "
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


def last_master(store, slug):
    """The newest master of the swarm, from the agent registry or the swarm history."""
    return getattr(_previous(store, slug), "agent", None)


def _previous(store, slug):
    current = [Previous(a, a.started_at) for a in store.agents(slug) if a.lane == MASTER]
    names = {p.agent.name for p in current}
    history = [json.loads(row) for row in store.redis.lrange(store.key(slug, "history"), 0, -1)]
    ended = [
        Previous(AgentRecord(**{k: v for k, v in row.items() if k in RECORD_FIELDS}), row["ended_at"])
        for row in history
        if row.get("lane") == MASTER and row["name"] not in names
    ]
    return max(current + ended, key=lambda p: p.ran_at, default=None)


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
    for _ in range(ASKS):
        found = ANSWERS.get(_answer(ask, CHOOSE))
        if found:
            return found
    raise SwarmError(f"no clear answer after {ASKS} tries; pass --last or --new")


def _profile(config):
    return config.lanes.get(MASTER, {}).get("profile") or DEFAULT_PROFILES[MASTER]


def _why(previous, config, live):
    if previous is None:
        return NONE_RECORDED
    if previous.agent.name in live:
        return "it is still running"
    if not previous.agent.harness:
        return "its harness is not recorded"
    return blocker(previous.agent, config.repo)


def _resume(store, slug, runtime, at, previous):
    config = store.ensure_code(slug)
    seat = seat_address(slug, MASTER)
    agent = replace(previous.agent, task=MASTER, seat=seat, profile=previous.agent.profile or _profile(config))
    text = RESUMED.format(slug=slug, name=agent.name, ledger=ledger_source(slug))
    placed = runtime.resume(config, agent, text)
    store.seats.occupy(seat, agent.name, at)
    record = replace(
        placed_record(agent, placed),
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
    """A saved launch with its empty keys filled: master profile, affinity or open seat harness, frontier model."""
    harness = saved.get("harness") or affinity.desired(config) or _open_seat()
    defaults = {"profile": _profile(config)}
    if harness:
        pick = model_pick.frontier(harness)
        defaults |= {
            "harness": harness,
            "model": pick.model,
            "effort": effort_range.clamp(harness, pick.effort, effort_range.of(config)),
        }
    return {**defaults, **{key: value for key, value in saved.items() if value}}


def _open_seat():
    harness, reason = agent_choice.choose("", dict(os.environ))
    return "" if reason == agent_choice.ALL_FULL else harness


def _filled(task, config):
    if "launch_assignment" in task:
        task = {**task, "launch_assignment": fill(task["launch_assignment"], config)}
    envelope = task.get("handoff_envelope") or {}
    if task.get("handoff") or envelope.get("launch"):
        task = {**task, "handoff_envelope": {**envelope, "launch": fill(envelope.get("launch") or {}, config)}}
    return task


def _new(store, slug, runtime, at):
    config = store.ensure_code(slug)
    name = store.next_name(slug, MASTER, at)
    record = AgentRecord(name, MASTER, MASTER, started_at=at, state="starting", seat=seat_address(slug, MASTER))
    pending = master_start.read(store, slug)
    store.put_agent(slug, record)
    try:
        store.seats.occupy(record.seat, name, at)
        transfer = transfers.attach(store, slug, record)
        task = {"id": MASTER, "handoff": store.handoff(slug, MASTER), "peer": store.peer(slug), "transfer": transfer}
        task = _filled(primed(store, slug, record.seat, task), config)
        master_start.begin(store, slug, name, task, at)
        affinity.handed_off(store, slug)
        if reader := getattr(runtime, "quota_capacity", None):
            reader(config, store.agents(slug), at / 1000)
        placed = runtime.spawn(config, MASTER, name, task)
    except Exception as exc:
        transfers.failed(store, slug, record)
        store.drop_agent(slug, name)
        affinity.failed(store, slug, str(exc))
        if pending:
            master_start.save(store, slug, pending)
        else:
            store.redis.delete(store.key(slug, "master-start"))
        raise SwarmError(f"the new master could not start: {exc}") from exc
    affinity.placed(store, slug, placed.harness)
    record = placed_record(record, placed)
    reported = runtime.reported(record)
    store.put_agent(slug, replace(record, state="working" if reported else "starting"))
    launch_check.begin(store, slug, record, at)
    if reported:
        store.redis.delete(store.key(slug, "master-start"))
        store.clear_handoff(slug, MASTER)
    store.redis.hdel(store.key(slug, "launch-assignments"), MASTER)
    return Launched(name, record.pane_id, record.seat, NEW)


def up(store, slug, runtime, at, choice, ask, say):
    """Launch the chosen master; ask last or new when no choice is given. Never refuses because a master is live."""
    live = runtime.live_names()
    previous = _previous(store, slug)
    asked = not choice
    if asked:
        say(describe(previous))
        say(QUESTION)
        choice = _choose(ask)
    if choice == NEW:
        return _new(store, slug, runtime, at)
    why = _why(previous, store.config(slug), live)
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
