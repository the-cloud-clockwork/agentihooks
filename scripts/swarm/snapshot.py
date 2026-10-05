"""Swarm snapshot and restore: one document in the swarm's state folder carrying its Redis state, ledger and worktrees.

A restore marks every agent whose pane is gone as finished, so the next tick retires it, reopens its task and
primes the successor with seat memory and handoffs, and leaves the swarm paused so only the master comes up.
"""

import json
import os
import subprocess
from dataclasses import replace
from pathlib import Path

from scripts.swarm.store import SwarmError

VERSION = 1


def path(slug):
    return Path.home() / ".agentihooks" / "swarm" / slug / "snapshot.json"


def ledger_path(slug):
    return Path(os.environ.get("LEDGER_DIR") or Path.home() / "development-ledger").expanduser() / f"{slug}.json"


def worktrees(repo, names, run=subprocess.run):
    """Each agent's worktree path: the worktree of repo checked out on the branch named after the agent."""
    proc = run(["git", "-C", repo, "worktree", "list", "--porcelain"], capture_output=True, text=True, timeout=30)
    found, current = {}, ""
    for line in proc.stdout.splitlines() if proc.returncode == 0 else []:
        if line.startswith("worktree "):
            current = line.removeprefix("worktree ")
        elif line.startswith("branch refs/heads/"):
            found[line.removeprefix("branch refs/heads/")] = current
    return {name: found.get(name, "") for name in names}


def take(store, slug, now_ms, run=subprocess.run):
    config, state = store.config(slug), store.export(slug)
    ledger = ledger_path(slug)
    doc = {
        "version": VERSION,
        "slug": slug,
        "taken_at": now_ms,
        "state": state,
        "ledger": json.loads(ledger.read_text(encoding="utf-8")) if ledger.exists() else None,
        "worktrees": worktrees(config.repo, [a.name for a in store.agents(slug)], run),
    }
    target = path(slug)
    target.parent.mkdir(parents=True, exist_ok=True)
    partial = target.with_suffix(".partial")
    partial.write_text(json.dumps(doc), encoding="utf-8")
    partial.chmod(0o600)
    partial.replace(target)
    return target


def restore(store, slug, live):
    """Write the snapshot back; returns the agents it marked finished."""
    try:
        doc = json.loads(path(slug).read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise SwarmError(f"no snapshot of swarm {slug}; take one with agentihooks swarm {slug} snapshot") from exc
    saved = doc["state"]["keys"].get(store.key(slug, "agents"), {}).get("value", {})
    running = sorted((set(saved) | {a.name for a in store.agents(slug)}) & set(live))
    if running:
        raise SwarmError(f"swarm {slug} still has live agents ({', '.join(running)}); stop it with stop --now first")
    store.restore(slug, doc["state"])
    finished = []
    for agent in store.agents(slug):
        if agent.state != "finished":
            store.put_agent(slug, replace(agent, state="finished"))
            finished.append(agent.name)
    store.update(slug, state="paused")
    ledger = ledger_path(slug)
    if doc["ledger"] is not None and not ledger.exists():
        ledger.parent.mkdir(parents=True, exist_ok=True)
        ledger.write_text(json.dumps(doc["ledger"]), encoding="utf-8")
    return finished
