"""Swarm snapshot and restore: documents in the swarm's state folder carrying its Redis state, ledger and worktrees.

Stop and the snapshot command write snapshot.json; while the swarm runs, the tick writes one automatic snapshot per
interval into the snapshots folder and keeps the newest ten. Restore takes the newest of them all unless pointed at one.

A restore reopens each agent in its own conversation where it can and leaves the swarm paused. Every other agent is
marked finished, so the next tick retires it, reopens its task and primes the successor with seat memory and handoffs.
"""

import json
import os
import subprocess
import time
from pathlib import Path

from scripts.swarm import naming, resume
from scripts.swarm.store import SwarmError

VERSION = 1
KEEP = 10
MINUTES = 30


def path(slug):
    root = Path.home() / ".agentihooks" / "swarm"
    if (root / slug).resolve().parent != root.resolve():
        raise SwarmError(f"refusing snapshot path for swarm {slug!r}: not one folder under {root}")
    return root / slug / "snapshot.json"


def auto_dir(slug):
    return path(slug).parent / "snapshots"


def automatic(slug):
    """The automatic snapshot files, oldest first."""
    return sorted(auto_dir(slug).glob("auto-[0-9]*.json"), key=_stamp)


def _stamp(file):
    return int(file.stem.removeprefix("auto-"))


def last_auto(slug):
    files = automatic(slug)
    return _stamp(files[-1]) if files else None


def interval_minutes(config, environ):
    """The swarm setting, else the environment, else the default; 0 turns automatic snapshots off."""
    minutes = config.snapshot_minutes
    if minutes is None:
        minutes = int(environ.get("AGENTIHOOKS_SWARM_SNAPSHOT_MINUTES") or MINUTES)
    return minutes


def auto(store, slug, now_ms, environ, run=subprocess.run):
    """Take an automatic snapshot when the running swarm's interval has passed; returns its file or None."""
    config, last = store.config(slug), last_auto(slug)
    minutes = interval_minutes(config, environ)
    if config.state != "running" or minutes <= 0 or (last is not None and now_ms - last < minutes * 60_000):
        return None
    taken = take(store, slug, now_ms, run, auto_dir(slug) / f"auto-{now_ms}.json")
    for old in automatic(slug)[:-KEEP]:
        old.unlink(missing_ok=True)
    return taken


def newest(slug):
    found = [p for p in (path(slug), *automatic(slug)) if p.exists()]
    if not found:
        raise SwarmError(f"no snapshot of swarm {slug}; take one with agentihooks swarm {slug} snapshot")
    return max(found, key=lambda p: json.loads(p.read_text(encoding="utf-8"))["taken_at"])


def recreate(store, slug, live):
    doc = json.loads(newest(slug).read_text(encoding="utf-8"))
    saved = doc["state"]["keys"].get(store.key(slug, "agents"), {}).get("value", {})
    if set(saved) & set(live):
        raise SwarmError(f"swarm {slug} still has live agents; wait for close to finish")
    store.restore(slug, doc["state"])
    for agent in store.agents(slug):
        store.release(slug, agent.task, agent.name)
        store.drop_agent(slug, agent.name)


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
    return {name: found.get(naming.plain(name), "") for name in names}


def take(store, slug, now_ms, run=subprocess.run, target=None):
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
    target = target or path(slug)
    target.parent.mkdir(parents=True, exist_ok=True)
    partial = target.with_suffix(".partial")
    partial.write_text(json.dumps(doc), encoding="utf-8")
    partial.chmod(0o600)
    partial.replace(target)
    return target


def restore(store, slug, live, source=None, runtime=None, has_quota=resume.account_quota, now_ms=None):
    """Write the snapshot back, the newest unless a source file is given; returns each agent's restore outcome."""
    source = Path(source) if source else newest(slug)
    try:
        doc = json.loads(source.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise SwarmError(f"no snapshot at {source}") from exc
    saved = doc["state"]["keys"].get(store.key(slug, "agents"), {}).get("value", {})
    running = sorted((set(saved) | {a.name for a in store.agents(slug)}) & set(live))
    if running:
        raise SwarmError(f"swarm {slug} still has live agents ({', '.join(running)}); stop it with stop --now first")
    store.restore(slug, doc["state"])
    store.update(slug, state="paused")
    ledger = ledger_path(slug)
    if doc["ledger"] is not None and not ledger.exists():
        ledger.parent.mkdir(parents=True, exist_ok=True)
        ledger.write_text(json.dumps(doc["ledger"]), encoding="utf-8")
    now_ms = int(time.time() * 1000) if now_ms is None else now_ms
    return resume.reopen(store, slug, doc["worktrees"], runtime, now_ms, ledger, has_quota)
