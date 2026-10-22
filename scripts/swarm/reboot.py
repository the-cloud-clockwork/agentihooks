"""Workstation reboot recovery: a planned shutdown records this hive's local agents, and the first tick of a new boot
reopens each recorded agent in its own conversation and seat; hive agents are never touched."""

import json
import os
import subprocess
from dataclasses import replace
from pathlib import Path

from scripts import herdr_host
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
    """Snapshot the swarm, record its local agents beside the snapshot and ask each master for its handoff."""
    taken = snapshot.take(store, slug, now_ms, run)
    agents = [a for a in store.agents(slug) if a.state != "finished"]
    doc = {
        "slug": slug,
        "taken_at": now_ms,
        "hive": commands.hive_id(),
        "snapshot": str(taken),
        "agents": [entry(a) for a in agents if local(a)],
    }
    target = record_path(slug)
    partial = target.with_suffix(".partial")
    partial.write_text(json.dumps(doc), encoding="utf-8")
    partial.replace(target)
    inbox = InboxStore(store.redis)
    for master in (a for a in agents if a.lane == MASTER):
        inbox.send(exits.BY, master.name, ASK.format(path=handoff_path(slug)))
    return doc


def _read(file):
    try:
        return json.loads(file.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None


def recorded(store, slug):
    """Agent names held by the newest of the planned record and the automatic snapshots; empty when neither exists."""
    planned, autos = _read(record_path(slug)), snapshot.automatic(slug)
    last = snapshot.last_auto(slug)
    if planned is not None and (last is None or planned["taken_at"] >= last):
        return {a["name"] for a in planned["agents"]}
    if not autos:
        return set()
    state = json.loads(autos[-1].read_text(encoding="utf-8"))["state"]
    return set(state["keys"].get(store.key(slug, "agents"), {}).get("value", {}))


def boot_id():
    return BOOT_ID.read_text(encoding="utf-8").strip()


def last_quota(harness, account):
    """The account's last quota reading however old, so a fresh boot routes before new readings arrive."""
    if harness != "claude":
        return resume.account_quota(harness, account)
    from scripts.claude_quota_balancer import account_cap, cached_observations

    seen = [result for _, result in cached_observations() if result.account == account]
    cap = account_cap(seen[-1]) if seen else None
    return None if cap is None else bool(cap)


def start_server():
    try:
        herdr_host.ensure_server(dict(os.environ))
    except herdr_host.HerdrError as error:
        return [f"herdr server did not start after the reboot: {error}"]
    return []


def boot_pass(slug, store, ledger, runtime, now_ms, current=None):
    """On the first tick of a new boot, reopen each recorded local agent that lost its process."""
    current = boot_id() if current is None else current
    key = store.key(slug, "boot", commands.hive_id())
    seen = store.redis.get(key)
    store.redis.set(key, current)
    if seen is None or seen == current or store.config(slug).state in {"stopped", "stopping"}:
        return []
    actions, wanted, live = start_server(), recorded(store, slug), runtime.live_names()
    for agent in store.agents(slug):
        if agent.name not in wanted or agent.name in live or agent.state == "finished" or not local(agent):
            continue
        if agent.account and last_quota(agent.harness, agent.account) is False:
            agent = replace(agent, account="")
            store.put_agent(slug, agent)
        reopened = dead_resume.reopen(slug, store, ledger, runtime, agent, now_ms)
        actions.append(f"{'resumed' if reopened else 'could not resume'} {agent.name} after the workstation rebooted")
    return actions
