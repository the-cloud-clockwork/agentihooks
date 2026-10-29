"""Seat the session that runs take-master as its swarm's master, retiring a live master only when told to."""

import json
import os
from dataclasses import replace
from pathlib import Path

from hooks.context.account_sessions import agent_pid
from hooks.context.broadcast import live_session_ids, name_session, pin_session
from hooks.context.master_coding import record_seat
from hooks.observability import agent_trace
from hooks.proc import processes
from scripts.handoff import transfers
from scripts.inbox.seats import seat_address
from scripts.swarm import launch_check, launch_model, naming
from scripts.swarm.store import MASTER, AgentRecord, SwarmError
from scripts.swarm_ledger import ledger_link
from scripts.swarm_v2 import masters as master_seats
from scripts.swarm_v2.runtime import process

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


def pin(slug, name, environ=os.environ):
    if ledger_link.remote(environ):
        environ.pop("AGENTIHOOKS_LEDGER_AGENT_TOKEN", None)
        environ.update(AGENTIHOOKS_SWARM=slug, AGENTIHOOKS_AGENT_NAME=name)


def hive_of(environ=os.environ):
    if ledger_link.local_mode(environ):
        return ""
    if not environ.get("AGENTIHOOKS_HIVE_ID"):
        raise SwarmError("take-master on a remote host needs AGENTIHOOKS_HIVE_ID from agentihooks hive add")
    return environ["AGENTIHOOKS_HIVE_ID"]


def placed(store, slug, record, hive, pid):
    previous = store.execution_occupants(slug).get(record.seat)
    target = process.launch_target(pid, process.local_namespace(), processes())
    if hive:
        target["host_id"] = hive
    record = replace(record, hive=hive, runtime_target=target)
    return store.start_execution(slug, record, previous.execution_id if previous else "")


def admitted(store, slug, record, admit, pid, hive):
    record = placed(store, slug, record, hive, pid)
    pin(slug, record.name)
    try:
        admit(record.name)
    except BaseException:
        store.execution_registry.update(slug, replace(record, state="ended"))
        store.drop_agent(slug, record.name)
        raise
    if hive:
        pin_session(pid, slug)
    return record


def retired(store, slug, runtime, others):
    for agent in others:
        if not runtime.retire(agent):
            raise SwarmError(f"could not retire {agent.name}; try again")
        store.drop_agent(slug, agent.name)


def master_record(slug, name, pid, now_ms):
    account, validated = launch_of(pid)
    harness = harness_of(pid) or validated.get("harness", "")
    model, effort = launch_model.read(harness, argv_of(pid)) if harness else ("", "")
    return AgentRecord(
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


def take(store, slug, name, runtime, now_ms, replace_live=False, admit=lambda name: None):
    hive = hive_of()
    live = runtime.live_names()
    carried, name = name, store.names.resolve(name)
    seated = _master_name(name, store.ensure_code(slug).code)
    own = {carried, name}
    masters = [a for a in store.agents(slug) if master_seats.is_lead(slug, a)]
    others = [a for a in masters if a.name not in own]
    running = [a.name for a in others if a.state != "finished" and a.name in live]
    if running and not replace_live:
        raise SwarmError(f"{running[0]} is the live master of {slug}; run take-master --replace to retire it first")
    pid = agent_pid()
    if not seated:
        name = store.next_name(slug, MASTER, now_ms)
    if not name_session(pid, name):
        raise SwarmError("this session is not registered with agentihooks, so the tick could not see it as master")
    before, after = (others, []) if hive else ([], others)
    retired(store, slug, runtime, before)
    record = admitted(store, slug, master_record(slug, name, pid, now_ms), admit, pid, hive)
    if not seated and carried and not store.names.entry(carried):
        store.names.alias(carried, name)
    retired(store, slug, runtime, after)
    for agent in masters:
        if agent.name in own and agent.name != name:
            store.drop_agent(slug, agent.name)
    store.seats.occupy(record.seat, name, now_ms)
    store.put_agent(slug, record)
    for session_id in live_session_ids(pid):
        agent_trace.record_seat(session_id, name, slug, MASTER, MASTER)
        record_seat(session_id, slug, name)
    launch_check.begin(store, slug, record, now_ms, relaunch=False)
    return record, transfers.attach(store, slug, record)
