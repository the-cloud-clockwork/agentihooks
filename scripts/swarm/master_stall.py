"""A master busy with no tool call past the stall threshold: the tick asks it for a handoff, then recycles it in its
seat with a recovery handoff. A proof swarm whose parent task is blocked stops itself instead."""

import re
from dataclasses import replace

from scripts.gates import quiet as quiet_gate
from scripts.handoff import envelope, transfers
from scripts.swarm import affinity, launch_check, master_retire, naming
from scripts.swarm.health import activity
from scripts.swarm.health import findings as health
from scripts.swarm.store import MASTER

STALLED = "stalled"
RESUMED = "the master resumed work"
REASON = "busy with no tool call for {minutes} minutes"
RECOVERY = """## Intent
Continue as the master of {slug}; {name} was busy with no tool call for {minutes} minutes and the swarm recycled it.

## Done
unknown: {name} left no handoff document before it stalled.

## Stopped at
unknown: {name} stalled inside a turn and never reported its last step.

## Decisions and promises
unknown: none recorded outside the ledger and the master inbox.

## Next
Re-read the ledger and the master inbox, then continue as master.

## Read first
- inbox:master

<!-- handoff complete -->
"""


def quiet_minutes(store, slug, master, now_ms):
    """Minutes the master has been busy with no tool call, None while its pane is not working, it is inside a tool
    call, it declared a wait or it is still starting."""
    from scripts.swarm.tick import STARTUP_GRACE_MS

    if store.redis.get(store.key(slug, "pane-state", master.name)) != "working":
        return None
    if quiet_gate.declared_wait(store.redis, slug, master.name, now_ms):
        return None
    last, done = activity.last_events(slug).get(master.name), activity.finished_events(slug).get(master.name)
    started = launch_check.session_started_at(master)
    if last is None or done is None or done < last or activity.open_calls(slug, master.name):
        return None
    if now_ms - started <= STARTUP_GRACE_MS:
        return None
    return (now_ms - max(done, started)) // health.MINUTE_MS


def parent_blocked(slug, store, ledger):
    """True for a proof swarm whose creating task in its parent swarm is blocked."""
    parent = naming.PROOF_RE.fullmatch(slug) and store.redis.hget(store.key(slug, "proof-creator"), "parent")
    if not parent:
        return False
    task = slug.split("-")[2]
    return any(
        re.sub(r"[^a-z0-9]", "", row["id"].lower()) == task and row.get("state") == "blocked"
        for row in ledger.state(parent)["tasks"]
    )


def run(slug, config, store, ledger, runtime, now_ms):
    master = affinity.live_master(store, slug)
    if master is None or master.lane != MASTER or config.state in {"stopping", "stopped"}:
        return []
    limit = health.limits().stalled_minutes
    minutes = quiet_minutes(store, slug, master, now_ms)
    held_for = master_retire.reason(store, slug, master.name)
    if minutes is None or minutes < limit:
        if held_for.startswith(REASON.split("{")[0]):
            master_retire.forget(store, slug, master.name, RESUMED)
        return []
    if parent_blocked(slug, store, ledger):
        store.update(slug, state="stopping")
        return [f"stopped proof swarm {slug}: its parent task is blocked and its master {master.name} stalled"]
    live = master.name in runtime.live_names()
    if held := master_retire.hold(store, slug, master, held_for or REASON.format(minutes=minutes), live, now_ms):
        return [held]
    recycle(slug, store, ledger, master, RECOVERY.format(slug=slug, name=master.name, minutes=minutes), now_ms)
    return [f"recycled master {master.name} in its seat after {minutes} minutes busy with no tool call"]


def recycle(slug, store, ledger, master, text, now_ms):
    found = envelope.build(store, slug, master, "recycle", ledger.state(slug)["tasks"], now_ms)
    store.put_handoff(slug, MASTER, text, seat=master.seat, envelope=found)
    transfers.record(store, slug, master, STALLED, text, now_ms)
    store.put_agent(slug, replace(master, state="finished"))
