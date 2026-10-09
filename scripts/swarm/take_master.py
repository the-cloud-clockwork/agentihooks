"""Seat the session that runs take-master as its swarm's master, retiring a live master only when told to."""

import json
import os
from pathlib import Path

from hooks.context.account_sessions import agent_pid
from hooks.context.broadcast import live_session_ids, name_session
from hooks.observability import agent_trace
from scripts.handoff import transfers
from scripts.inbox.seats import seat_address
from scripts.swarm import launch_check, launch_model, naming
from scripts.swarm.store import MASTER, AgentRecord, SwarmError

PROC = Path("/proc")


def harness_of(pid):
    from hooks.proc import _process, _target

    process = _process(pid, Path("/proc"))
    return _target(process) if process else ""


def argv_of(pid):
    from hooks.proc import _process

    process = _process(pid, Path("/proc"))
    return process.argv if process else ()


def launch_of(pid):
    from scripts.profiles import binding

    try:
        found, _, env, account = binding.process(PROC, pid)
    except (OSError, ValueError, StopIteration):
        return "", {}
    try:
        report = json.loads(Path(env[binding.REPORT]).read_text())
        validated = report["state"] == "validated" and report["validation"]["pid"] == found
    except (OSError, ValueError, KeyError):
        return account, {}
    return account, report["validation"] if validated else {}


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
    if not seated and carried and not store.names.entry(carried):
        store.names.alias(carried, name)
    for agent in others:
        if not runtime.retire(agent):
            raise SwarmError(f"could not retire {agent.name}; try again")
        store.drop_agent(slug, agent.name)
    for agent in masters:
        if agent.name in own and agent.name != name:
            store.drop_agent(slug, agent.name)
    account, validated = launch_of(pid)
    harness = harness_of(pid) or validated.get("harness", "")
    model, effort = launch_model.read(harness, argv_of(pid)) if harness else ("", "")
    record = AgentRecord(
        name,
        MASTER,
        MASTER,
        harness=harness,
        profile=validated.get("profile") or os.environ.get("AGENTIHOOKS_PROFILE", ""),
        started_at=now_ms,
        model=validated.get("model") or model,
        effort=validated.get("effort") or effort,
        account=account,
        seat=seat_address(slug, MASTER),
        profile_decision={"validation": validated} if validated else {},
    )
    store.seats.occupy(record.seat, name, now_ms)
    store.put_agent(slug, record)
    for session_id in live_session_ids(pid):
        agent_trace.record_seat(session_id, name, slug, MASTER, MASTER)
    launch_check.begin(store, slug, record, now_ms, relaunch=False)
    return record, transfers.attach(store, slug, record)
