"""The Stop gate for a swarm worker holding a claim: the stop passes only on a wait the tick can check or end."""

import json

from scripts.gates import log
from scripts.gates.base import Decision
from scripts.swarm.naming import lane_of
from scripts.swarm_ledger import ledger_kinds

WORKERS = frozenset({"eng", "ci"})
ACTIVE = frozenset({"claimed", "pr"})
STREAK = 3
CHECKS_WAIT_MS = 12 * 60 * 60_000
KEPT = frozenset({"reply", "task"})
PLAIN = {
    "merged": "its pull request had merged and the task stayed open",
    "red": "its checks had failed and nothing was pushed",
    "green": "its checks had passed and the pull request was not merged",
    "idle": "it held the task with no pull request and no wait",
    "contract": "its pull request had merged and its proof contract was still open",
}
COMMAND = '--command "<command>" --output "<its output>"'
CONTRACT_PROOF = {
    "ops": COMMAND,
    "tune": COMMAND,
    "troubleshoot": '--root-cause "<cause>" --evidence "<what shows it>" --fix <pr url> | --filed "<follow up>"',
    "research": "--finding <link>",
}


def ruling(task, pull, waiting):
    """The work this stop leaves owed ('' when it may pass), and the pull request whose pending checks become the wait."""
    url = task.get("pr_url")
    if url and pull is None:
        return "", ""
    if pull is not None and pull.state == "MERGED":
        if ledger_kinds.kind(task) not in CONTRACT_PROOF:
            return "merged", ""
        if not waiting or waiting.get("on") == {"kind": "checks", "target": url}:
            return "contract", ""
    if pull is not None and pull.state == "OPEN":
        if not pull.resolved:
            return "", url
        if pull.red and not pull.gate_passed:
            return "red", ""
    if waiting:
        return "", ""
    if pull is not None and pull.state == "OPEN":
        return "green", ""
    return "idle", ""


def parked_by(store, who, task):
    if not task.get("parked_on"):
        return False
    return any(a.name == who.name and a.state == "finished" for a in store.agents(who.swarm))


def refusal(owed, slug, task, pull):
    url, block = task.get("pr_url"), f'agentihooks swarm {slug} block "<why>"'
    name_wait = (
        f"Name the wait: agentihooks swarm {slug} wait --on checks <pr url> | reply <inbox item> | task <id>, or a bare "
        f"wait of at most 60 minutes; or block with {block}"
    )
    if owed == "merged":
        return f"your pull request {url} merged: close the task now with agentihooks swarm {slug} done --pr {url}"
    if owed == "contract":
        kind = ledger_kinds.kind(task)
        return (
            f"your pull request {url} merged, and {kind} task {task['id']} closes on its proof contract: close it with "
            f"agentihooks swarm {slug} done {CONTRACT_PROOF[kind]}. {name_wait}"
        )
    if owed == "red":
        return (
            f"checks failed on {url}: {', '.join(pull.failed) or 'a check'}. Fix them and push, or block with {block}"
        )
    if owed == "green":
        return f"checks passed on {url}: merge it, then run agentihooks swarm {slug} done --pr {url}"
    return f"you hold task {task['id']} with no open pull request and no wait. {name_wait}"


class ClaimStop:
    name = "claim-stop"
    default_mode = "enforce"

    def __init__(self, connect=None, ledger=None, github=None, now=None, streak=STREAK):
        self._connect, self._ledger, self._github, self._now, self.streak = connect, ledger, github, now, streak

    def matches(self, call):
        return not call.tool

    def decide(self, call, who, state):
        if not (who.pinned and who.task) or lane_of(who.name) not in WORKERS:
            return Decision()
        ledger = self.ledger()
        task = next((t for t in ledger.tasks(who.swarm) if t.get("id") == who.task), None)
        if task is None or task.get("state") not in ACTIVE or task.get("claimed_by") != who.name:
            return Decision()
        from scripts.swarm import idle

        store, now = self.connect(), self.now()
        if parked_by(store, who, task):
            return Decision()
        url = task.get("pr_url")
        held = idle.wait(store.redis, who.swarm, who.name)
        live = held if held and held["until"] > now else None
        pull = self.github()(url) if url else None
        owed, checks = ruling(task, pull, live)
        if checks and (live or {}).get("on", {}).get("kind") not in KEPT:
            on = {"kind": "checks", "target": checks}
            idle.declare_wait(store.redis, who.swarm, who.name, now + CHECKS_WAIT_MS, f"checks on {checks}", now, on=on)
        streak = Streak(store, who.swarm, who.name)
        if not owed:
            streak.clear()
            return Decision()
        reason, count = refusal(owed, who.swarm, task, pull), streak.bump(now)
        if count < self.streak:
            return Decision.deny(f"{reason} (stop block {count} of {self.streak - 1}; the next one blocks the task)")
        streak.clear()
        block_task(
            store, ledger, who, f"Blocked by the stop gate: the agent stopped {count} times while {PLAIN[owed]}."
        )
        log.append(state.slug, log.Row.of(self.name, "blocked", who, call.tool, reason), state.home)
        return Decision()

    def connect(self):
        if self._connect:
            return self._connect()
        from scripts.swarm.store import connect

        return connect()

    def ledger(self):
        if self._ledger:
            return self._ledger()
        from scripts.swarm.ledger_client import LedgerClient

        return LedgerClient()

    def github(self):
        if self._github:
            return self._github
        from scripts.swarm.ledger_events import view

        return view

    def now(self):
        if self._now:
            return self._now()
        import time

        return int(time.time() * 1000)


class Streak:
    """Stop blocks in a row: an outcome on the one progress signal since the last block starts the count again."""

    def __init__(self, store, slug, name):
        self.store, self.slug, self.name = store, slug, name
        self.key = store.key(slug, "stop-blocks", name)

    def bump(self, now):
        from scripts.gates.progress import Progress

        held = json.loads(self.store.redis.get(self.key) or '{"count": 0, "at": 0}')
        outcome_at = Progress(self.store.redis, self.slug).read(self.name).outcome_at
        count = held["count"] if outcome_at <= held["at"] else 0
        self.store.redis.set(self.key, json.dumps({"count": count + 1, "at": now}))
        return count + 1

    def clear(self):
        self.store.redis.delete(self.key)


def block_task(store, ledger, who, note):
    from scripts.swarm.cli import block_agent
    from scripts.swarm.store import AgentRecord

    found = next((a for a in store.agents(who.swarm) if a.name == who.name), None)
    block_agent(store, who.swarm, found or AgentRecord(who.name, lane_of(who.name), who.task), note, ledger)
