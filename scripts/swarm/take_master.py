"""Seat the session that runs take-master as its swarm's master, retiring a live master only when told to."""

import os
from pathlib import Path

from hooks.context.account_sessions import agent_pid
from hooks.context.broadcast import name_session
from scripts.handoff import transfers
from scripts.inbox.seats import seat_address
from scripts.swarm import launch_model, naming
from scripts.swarm.store import MASTER, AgentRecord, SwarmError


def harness_of(pid):
    from hooks.proc import _process, _target

    process = _process(pid, Path("/proc"))
    return (_target(process) if process else "") or "claude"


def argv_of(pid):
    from hooks.proc import _process

    process = _process(pid, Path("/proc"))
    return process.argv if process else ()


def _master_name(name, code):
    parsed = naming.parse(name)
    return bool(parsed) and parsed.kind == MASTER and parsed.code == code


def take(store, slug, name, runtime, now_ms, replace_live=False):
    live = runtime.live_names()
    carried, name = name, store.names.resolve(name)
    seated = _master_name(name, store.ensure_code(slug).code)
    own = {carried, name}
    masters = [a for a in store.agents(slug) if a.lane == MASTER]
    others = [a for a in masters if a.name not in own]
    running = [a.name for a in others if a.state != "finished" and a.name in live]
    if running and not replace_live:
        raise SwarmError(f"{running[0]} is the live master of {slug}; run take-master --replace to retire it first")
    pid = agent_pid()
    if not seated:
        name = store.next_name(slug, MASTER, now_ms)
        if not name_session(pid, name):
            raise SwarmError("this session is not registered with agentihooks, so the tick could not see it as master")
        if carried:
            store.names.alias(carried, name)
    for agent in others:
        if not runtime.retire(agent, agent.name in live):
            raise SwarmError(f"could not retire {agent.name}; try again")
        store.drop_agent(slug, agent.name)
    for agent in masters:
        if agent.name in own and agent.name != name:
            store.drop_agent(slug, agent.name)
    harness = harness_of(pid)
    model, effort = launch_model.read(harness, argv_of(pid))
    record = AgentRecord(
        name,
        MASTER,
        MASTER,
        harness=harness,
        profile=os.environ.get("AGENTIHOOKS_PROFILE", ""),
        started_at=now_ms,
        model=model,
        effort=effort,
        seat=seat_address(slug, MASTER),
    )
    store.seats.occupy(record.seat, name, now_ms)
    store.put_agent(slug, record)
    return record, transfers.attach(store, slug, record)
