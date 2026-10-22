"""Workstation reboot recovery: a planned shutdown records this hive's local agents, and the first tick of a new boot
reopens each recorded agent in its own conversation and seat; hive agents are never touched."""

import json
import os
import subprocess
import tempfile
import time
from dataclasses import asdict, replace
from pathlib import Path

from scripts import herdr_host
from scripts.handoff import transfers
from scripts.hive import reports
from scripts.inbox import exits
from scripts.inbox.store import InboxStore
from scripts.swarm import commands, dead_resume, resume, snapshot
from scripts.swarm.store import MASTER

RECORD = "reboot.json"
HANDOFF = "master-shutdown-handoff.md"
BOOT_ID = Path("/proc/sys/kernel/random/boot_id")
FIELDS = (
    "name",
    "lane",
    "seat",
    "task",
    "conversation_id",
    "profile",
    "overlays",
    "harness",
    "model",
    "effort",
    "account",
)
ASK = (
    "A planned shutdown of this workstation follows. Write your Handoff v2 now with the handoff skill and save it to "
    "{path}. Do not submit it and do not stop: the swarm reopens this conversation in your seat after the boot."
)


def record_path(slug):
    return snapshot.path(slug).parent / RECORD


def handoff_path(slug):
    return snapshot.path(slug).parent / HANDOFF


def local(agent):
    return agent.runtime_backend == "local" and not reports.host(agent)


def entry(agent):
    return {field: getattr(agent, field) for field in FIELDS}


def prepare(store, slug, now_ms, run=subprocess.run):
    """Record the swarm's local agents and ask each master for its handoff, then snapshot the swarm.

    The record lands first because a full snapshot can take minutes and the shutdown may not wait for it."""
    agents = [a for a in store.agents(slug) if a.state != "finished"]
    doc = {
        "slug": slug,
        "taken_at": now_ms,
        "hive": commands.hive_id(),
        "boot": boot_id(),
        "snapshot": str(snapshot.path(slug)),
        "agents": [entry(a) for a in agents if local(a)],
    }
    target = record_path(slug)
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", dir=target.parent, delete=False) as partial:
        json.dump(doc, partial)
    Path(partial.name).replace(target)
    inbox = InboxStore(store.redis)
    for master in (a for a in agents if a.lane == MASTER and local(a)):
        inbox.send(exits.BY, master.name, ASK.format(path=handoff_path(slug)))
    snapshot.take(store, slug, now_ms, run)
    return doc


def _read(file):
    try:
        return json.loads(file.read_text())
    except FileNotFoundError:
        return None


def recorded(store, slug, ended):
    """Agent names held by the newest of the automatic snapshots and the planned record taken in the boot that ended;
    empty when neither exists."""
    planned, autos = _read(record_path(slug)), snapshot.automatic(slug)
    last = snapshot.last_auto(slug)
    if planned is not None and planned.get("boot") == ended and (last is None or planned["taken_at"] >= last):
        return {a["name"] for a in planned["agents"]}
    newest = _read(autos[-1]) if autos else None
    if newest is None:
        return set()
    return set(newest["state"]["keys"].get(store.key(slug, "agents"), {}).get("value", {}))


def boot_id():
    return BOOT_ID.read_text().strip()


def last_quota(harness, account, now=None):
    """The account's last quota reading, reused past the freshness window until one of its windows resets, so a
    fresh boot routes before new readings arrive."""
    if harness != "claude":
        return resume.account_quota(harness, account)
    from scripts import session_bands
    from scripts.claude_quota_balancer import account_cap, cached_observations

    now = time.time() if now is None else now
    seen = [(at, result) for at, result in cached_observations() if result.account == account]
    if not seen:
        return None
    at, result = max(seen, key=lambda reading: reading[0])
    resets = [w.resets_at for w in (result.five_hour, result.seven_day)]
    if not session_bands.fresh(at, now) and not all(r is not None and r > now for r in resets):
        return None
    cap = account_cap(result, now)
    return None if cap is None else bool(cap)


def start_server():
    try:
        herdr_host.ensure_server(dict(os.environ))
    except herdr_host.HerdrError as error:
        return [f"herdr server did not start after the reboot: {error}"]
    return []


def boot_pass(slug, store, ledger, runtime, now_ms, current=None):
    """On the first tick of a new boot, reopen each recorded local agent that lost its process.

    The boot counts as handled once the herdr server answered, so a failed start retries on the next tick."""
    current = boot_id() if current is None else current
    key = store.key(slug, "boot", commands.hive_id())
    seen = store.redis.get(key)
    if seen == current:
        return []
    wanted, live = recorded(store, slug, seen), runtime.live_names()
    lost = [
        a for a in store.agents(slug) if a.name in wanted and a.name not in live and a.state != "finished" and local(a)
    ]
    if seen is None or not lost or store.config(slug).state in {"stopped", "stopping"}:
        store.redis.set(key, current)
        return []
    actions = start_server()
    if actions:
        return actions
    store.redis.set(key, current)
    for agent in lost:
        routed = agent
        if agent.account and last_quota(agent.harness, agent.account) is not True:
            routed = replace(agent, account="")
            store.put_agent(slug, routed)
        if not dead_resume._reason(store, slug, routed) and dead_resume.reopen(
            slug, store, ledger, runtime, routed, now_ms, notice=""
        ):
            actions.append(f"resumed {agent.name} after the workstation rebooted")
        elif dead_resume._owns(store, slug, routed, ledger):
            actions.append(_await_decision(slug, store, ledger, agent, now_ms))
    return actions


def _await_decision(slug, store, ledger, agent, now_ms):
    """A failed reboot resume waits for the master or operator to choose resume or fresh, never a silent fresh start;
    the agent keeps its recorded account for that choice."""
    shutdown = _shutdown_handoff(slug) if agent.lane == MASTER else ""
    text = store.handoff(slug, agent.task) or shutdown or transfers.last_handoff(store, slug, agent.task)
    transfer = transfers.record(store, slug, agent, "restore", text, now_ms)
    store.put_agent(slug, replace(agent, state=resume.AWAITING))
    outcome = resume.Outcome(
        agent.name,
        agent.lane,
        agent.task,
        resume.AWAITING,
        "own conversation did not reopen after the workstation rebooted",
        agent.conversation_id,
        now_ms,
        transfer["id"],
    )
    kept = [r for r in store.restored(slug) if r["name"] != agent.name]
    store.put_restored(slug, [*kept, asdict(outcome)])
    action = f"{agent.name} awaits a resume or fresh decision after the workstation rebooted"
    ledger.notify(slug, f"{action}; nothing was started in its place.")
    return action


def _shutdown_handoff(slug):
    try:
        return handoff_path(slug).read_text()
    except FileNotFoundError:
        return ""
