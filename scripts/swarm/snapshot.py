"""Swarm snapshot and restore: documents in the swarm's state folder carrying its Redis state, ledger and worktrees.

Stop and the snapshot command write snapshot.json; while the swarm runs, the tick writes one automatic snapshot per
interval into the snapshots folder and keeps the newest ten. Restore takes the newest of them all unless pointed at one.

A restore reopens each agent in its own conversation where it can and leaves the swarm paused. Every other agent is
marked finished, so the next tick retires it, reopens its task and primes the successor with seat memory and handoffs.
"""

import hashlib
import json
import os
import subprocess
import time
from collections.abc import Callable, Mapping
from pathlib import Path

from scripts.profiles import binding
from scripts.swarm import naming, resume
from scripts.swarm.store import AgentRecord, SwarmError
from scripts.swarm_v2.checkpoints import git_capture, native

VERSION = 1
KEEP = 10
MINUTES = 30
SOURCE_SWITCH = "AGENTIHOOKS_SWARM_SOURCE_CAPTURE"
SOURCE_REF = "AGENTIHOOKS_SWARM_SOURCE_REF"
NATIVE_SWITCH = "AGENTIHOOKS_SWARM_NATIVE_CAPTURE"


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
    taken = take(store, slug, now_ms, run, auto_dir(slug) / f"auto-{now_ms}.json", environ)
    for old in automatic(slug)[:-KEEP]:
        old.unlink(missing_ok=True)
    prune_sources(slug)
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


def ledger_source(slug):
    """How an agent reads the ledger, as its prompts name it."""
    from scripts.swarm.prompt import ledger_read

    return ledger_read(slug)


def stored_ledger(slug):
    from scripts.swarm_ledger.repository import repository

    return repository.export_document(slug) if repository.exists(slug) else None


def worktrees(repo, names, run=subprocess.run):
    """Each agent's worktree path: the worktree of repo checked out on the branch named after the agent."""
    try:
        proc = run(["git", "-C", repo, "worktree", "list", "--porcelain"], capture_output=True, text=True, timeout=30)
    except FileNotFoundError:
        return dict.fromkeys(names, "")
    found, current = {}, ""
    for line in proc.stdout.splitlines() if proc.returncode == 0 else []:
        if line.startswith("worktree "):
            current = line.removeprefix("worktree ")
        elif line.startswith("branch refs/heads/"):
            found[line.removeprefix("branch refs/heads/")] = current
    return {name: found.get(naming.plain(name), "") for name in names}


def sources_dir(slug):
    return path(slug).parent / "sources"


def _keep(folder, data):
    """Objects are named by their sha256, so a torn write is found by its name and written again."""
    target = folder / hashlib.sha256(data).hexdigest()
    if target.exists() and hashlib.sha256(target.read_bytes()).hexdigest() == target.name:
        return
    folder.mkdir(parents=True, exist_ok=True)
    target.touch(mode=0o600)
    target.write_bytes(data)


def sources(slug, found, environ):
    """Each agent worktree's committed, staged, unstaged and permitted untracked work, kept beside its path.

    The switch set to 0 keeps paths only, as before; restore still reopens the saved worktree paths."""
    if environ.get(SOURCE_SWITCH) == "0":
        return {}
    ref, captured = environ.get(SOURCE_REF) or "origin/dev", {}
    for name, worktree in found.items():
        if not worktree:
            continue
        try:
            source = git_capture.capture(Path(worktree), ref)
        except git_capture.SourceError as error:
            captured[name] = {"error": str(error)}
            continue
        for data in source.materials.values():
            _keep(sources_dir(slug), data)
        captured[name] = source.fields()
    return captured


def native_home(agent: AgentRecord, environ: Mapping[str, str]) -> Path:
    """The home the agent was validated in, else the harness's own variable, else its default under the user home."""
    recorded = agent.profile_decision.get("validation", {}).get("home")
    if recorded:
        return Path(recorded)
    return Path(environ.get(binding.HOMES[agent.harness]) or Path.home() / f".{agent.harness}")


def cli_version(harness: str, run: Callable) -> str:
    try:
        proc = run([harness, "--version"], capture_output=True, text=True, errors="replace", timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return proc.stdout if proc.returncode == 0 else ""


def _native(agent, home, cwds, version):
    """Claude keys a conversation by its launch folder, the agent's worktree or the swarm repo; Codex ignores it."""
    found = native.Unavailable(agent.harness, "conversation_absent")
    for cwd in cwds:
        found = native.capture(agent.harness, home, agent.conversation_id, Path(cwd), version, stopped=False)
        if isinstance(found, native.NativeCapture) or found.cause != "conversation_absent":
            return found
    return found


def natives(
    slug: str, agents: list[AgentRecord], repo: str, found: dict[str, str], environ: Mapping[str, str], run: Callable
) -> dict[str, dict]:
    """Each agent's conversation files as its installed CLI wrote them, kept by hash; never its account state.

    The switch set to 0 records none, and restore reopens conversations from the CLI homes as before."""
    if environ.get(NATIVE_SWITCH) == "0":
        return {}
    versions, captured = {}, {}
    for agent in agents:
        if agent.harness not in native.HARNESSES or not agent.conversation_id:
            continue
        if agent.harness not in versions:
            versions[agent.harness] = cli_version(agent.harness, run)
        cwds = list(dict.fromkeys(p for p in (found.get(agent.name), repo) if p))
        result = _native(agent, native_home(agent, environ), cwds, versions[agent.harness])
        if isinstance(result, native.NativeCapture):
            _keep(sources_dir(slug), result.material())
        captured[agent.name] = result.fields()
    return captured


def referenced(slug):
    """Every source object a kept snapshot still names."""
    held = set()
    for file in (path(slug), *automatic(slug)):
        if file.exists():
            doc = json.loads(file.read_bytes())
            for fields in (*doc.get("sources", {}).values(), *doc.get("natives", {}).values()):
                held.update(ref["sha256"] for ref in fields.get("inventory", {}).values())
    return held


def prune_sources(slug):
    held = referenced(slug)
    for file in sources_dir(slug).glob("*"):
        if file.name not in held:
            file.unlink()


def take(store, slug, now_ms, run=subprocess.run, target=None, environ=None):
    config, state, agents = store.config(slug), store.export(slug), store.agents(slug)
    environ = os.environ if environ is None else environ
    found = worktrees(config.repo, [a.name for a in agents], run)
    doc = {
        "version": VERSION,
        "slug": slug,
        "taken_at": now_ms,
        "state": state,
        "ledger": stored_ledger(slug),
        "worktrees": found,
        "sources": sources(slug, found, environ),
        "natives": natives(slug, agents, config.repo, found, environ, run),
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
    if doc["ledger"] is not None:
        from scripts.swarm_ledger.repository import repository

        if not repository.exists(slug):
            repository.import_document(slug, doc["ledger"])
    now_ms = int(time.time() * 1000) if now_ms is None else now_ms
    return resume.reopen(store, slug, doc["worktrees"], runtime, now_ms, ledger_source(slug), has_quota)
