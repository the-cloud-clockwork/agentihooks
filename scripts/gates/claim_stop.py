"""The Stop gate for a swarm worker holding a claim: the stop passes only on a wait the tick can check or end."""

import json
from collections.abc import Callable
from typing import TYPE_CHECKING, Any, cast

from scripts.gates import lift, log
from scripts.gates.base import Call, Decision, Who
from scripts.hive import scope
from scripts.swarm.naming import lane_of
from scripts.swarm_ledger import ledger_kinds

if TYPE_CHECKING:
    from scripts.gates.verdicts import Verdicts
    from scripts.swarm.ledger_client import LedgerClient
    from scripts.swarm.ledger_events import PullRequest
    from scripts.swarm.store import AgentRecord, RedisStore

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


def ruling(
    task: dict[str, Any], pull: "PullRequest | None", waiting: dict[str, Any] | None, dependency: bool = False
) -> tuple[str, str]:
    """The work this stop leaves owed ('' when it may pass), and the pull request whose pending checks become the wait."""
    url: str | None = task.get("pr_url")
    if url and pull is None:
        return "", ""
    if pull is not None and pull.state == "MERGED":
        if cast(Callable[[dict[str, Any]], str], ledger_kinds.kind)(task) not in CONTRACT_PROOF and not cast(
            Callable[[dict[str, Any]], bool], ledger_kinds.after
        )(task):
            return "merged", ""
        if not waiting or waiting.get("on") == {"kind": "checks", "target": url}:
            return "contract", ""
    if dependency:
        return "", ""
    if pull is not None and pull.state == "OPEN":
        if not pull.resolved:
            return "", cast(str, url)
        if pull.red and not pull.gate_passed:
            return "red", ""
    if waiting:
        return "", ""
    if pull is not None and pull.state == "OPEN":
        return "green", ""
    return "idle", ""


def between_halves(task: dict[str, Any], pull: "PullRequest | None") -> bool:
    pulls: list[Any] = task.get("split_pulls") or []
    split: list[Any] = task.get("split") or []
    return pull is not None and pull.state == "MERGED" and task["pr_url"] in pulls and len(pulls) < len(split)


def named(live: dict[str, Any] | None, url: str | None) -> str:
    on: dict[str, Any] = (live or {}).get("on") or {}
    target = on.get("target")
    return cast(str, target) if on.get("kind") == "checks" and target != url else ""


def named_ruling(pull: "PullRequest | None") -> str:
    if pull is not None and pull.state == "OPEN" and pull.resolved and pull.red and not pull.gate_passed:
        return "red"
    return ""


def parked_by(store: "RedisStore", who: Who, task: dict[str, Any]) -> bool:
    if not task.get("parked_on"):
        return False
    agents = cast(Callable[[str], list["AgentRecord"]], store.agents)(who.swarm)
    return any(a.name == who.name and a.state == "finished" for a in agents)


def accepted_handoff(store: "RedisStore", who: Who) -> bool:
    from scripts.handoff import transfers

    agents = cast(Callable[[str], list["AgentRecord"]], store.agents)(who.swarm)
    if not any(agent.name == who.name and agent.task == who.task and agent.state == "finished" for agent in agents):
        return False
    return any(
        row["predecessor"] == who.name and row["task"] == who.task and row["handoff"]
        for row in cast(Callable[["RedisStore", str], list[dict[str, Any]]], transfers.list_transfers)(store, who.swarm)
    )


def release_verification(task: dict[str, Any], store: "RedisStore", ledger: "LedgerClient", who: Who, now: int) -> bool:
    from dataclasses import replace

    if task.get("awaiting") != "verify":
        return False
    contract: dict[str, Any] = task.get("contract") or {}
    at = contract.get("after_at")
    if not at or int(at) <= now:
        return False
    expected: dict[str, Any] = {key: task.get(key, "") for key in ("claimed_by", "pr_url", "awaiting")}
    expected["claim_generation"] = task.get("claim_generation", 0)
    released = cast(Callable[..., dict[str, Any]], ledger.update_task)(
        who.swarm,
        who.task,
        {"state": "open", "claimed_by": ""},
        if_state=ACTIVE,
        if_match=expected,
    )
    if (
        released.get("state") != "open"
        or released.get("claimed_by")
        or released.get("claim_generation") != expected["claim_generation"] + 1
    ):
        return False
    store.release(who.swarm, who.task, who.name)
    agents = cast(Callable[[str], list["AgentRecord"]], store.agents)(who.swarm)
    for agent in agents:
        if agent.name == who.name:
            cast(Callable[[str, "AgentRecord"], None], store.put_agent)(who.swarm, replace(agent, state="finished"))
    return True


def refusal(owed: str, slug: str, task: dict[str, Any], pull: "PullRequest | None") -> str:
    url, block = task.get("pr_url"), f'agentihooks swarm {slug} block "<why>"'
    name_wait = (
        f"Name the wait: agentihooks swarm {slug} wait --on checks <pr or Actions run url> | reply <inbox item> | task <id>, or a bare "
        f"wait of at most 60 minutes; or block with {block}"
    )
    if owed == "merged":
        return f"your pull request {url} merged: close the task now with agentihooks swarm {slug} done --pr {url}"
    if owed == "contract":
        kind = cast(Callable[[dict[str, Any]], str], ledger_kinds.kind)(task)
        after = cast(Callable[[dict[str, Any]], bool], ledger_kinds.after)(task)
        proof = CONTRACT_PROOF["ops"] if after else CONTRACT_PROOF[kind]
        return (
            f"your pull request {url} merged, and {kind} task {task['id']} closes on its proof contract: close it with "
            f"agentihooks swarm {slug} done {proof}. {name_wait}"
        )
    if owed == "red":
        assert pull is not None
        failed = cast(tuple[str, ...], pull.failed)
        return f"checks failed on {url}: {', '.join(failed) or 'a check'}. Fix them and push, or block with {block}"
    if owed == "green":
        return f"checks passed on {url}: merge it, then run agentihooks swarm {slug} done --pr {url}"
    return f"you hold task {task['id']} with no open pull request and no wait. {name_wait}"


def dependency_rows(ledger: "LedgerClient", slug: str, held: dict[str, Any]) -> dict[str, dict[str, Any]]:
    target = held.get("target") if held.get("kind") == "task" else None
    row = cast(Callable[[str, str], dict[str, Any] | None], ledger.task)(slug, target) if target else None
    return {cast(str, target): row} if row else {}


class ClaimStop:
    name = "claim-stop"
    default_mode = "enforce"

    def __init__(
        self,
        connect: Callable[[], "RedisStore"] | None = None,
        ledger: Callable[[], "LedgerClient"] | None = None,
        github: Callable[..., "PullRequest | None"] | None = None,
        now: Callable[[], int] | None = None,
        streak: int = STREAK,
    ) -> None:
        self._connect, self._ledger, self._github, self._now, self.streak = connect, ledger, github, now, streak

    def matches(self, call: Call) -> bool:
        return not call.tool

    def decide(self, call: Call, who: Who, state: "Verdicts") -> Decision:
        if not (who.pinned and who.task) or lane_of(who.name) not in WORKERS:
            return Decision()
        from scripts.swarm import outage

        store, now = self.connect(), self.now()
        streak = Streak(store, who.swarm, who.name)
        if accepted_handoff(store, who) or any(
            record["kind"] == "ledger"
            for record in cast(Callable[[str], list[dict[str, Any]]], outage.open_records)(who.swarm)
        ):
            streak.clear()
            return Decision()
        ledger = self.ledger()
        task: dict[str, Any] | None = cast(Callable[[str, str], "dict[str, Any] | None"], ledger.task)(
            who.swarm, who.task
        )
        if task is None or task.get("state") not in ACTIVE or task.get("claimed_by") != who.name:
            return Decision()
        from scripts.swarm import idle, ready, waits

        if (
            lift.lifted(who.swarm, call.session, self.name, state.home)
            or lift.task_lifted(who.swarm, who.task, self.name, state.home)
            or (
                not lift.task_lift_path(who.swarm, who.task, self.name, state.home).exists()
                and lift.agent_lifted(who.swarm, who.name, self.name, state.home)
            )
        ):
            streak.clear()
            return Decision()
        if release_verification(task, store, ledger, who, now):
            streak.clear()
            return Decision()
        if parked_by(store, who, task) or cast(Callable[[dict[str, Any]], bool], ready.awaiting)(task):
            return Decision()
        held = cast(Callable[[Any, str, str], "dict[str, Any] | None"], idle.wait)(
            cast(Any, store.redis), who.swarm, who.name
        )
        live = held if held and held["until"] > now else None
        task, pull, owed, checks = self.grade(ledger, who, task, live)
        held_on = (live or {}).get("on", {})
        run_wait = held_on.get("kind") == "checks" and waits.mutation_wait.RUN_URL.fullmatch(held_on["target"])
        if checks and held_on.get("kind") not in KEPT and not run_wait:
            on: dict[str, Any] = {"kind": "checks", "target": checks}
            cast(Callable[[Any, str, str, int, str, int, dict[str, Any]], None], idle.declare_wait)(
                cast(Any, store.redis), who.swarm, who.name, now + CHECKS_WAIT_MS, f"checks on {checks}", now, on
            )
        if live or not owed:
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

    def grade(
        self, ledger: "LedgerClient", who: Who, task: dict[str, Any], live: dict[str, Any] | None
    ) -> tuple[dict[str, Any], "PullRequest | None", str, str]:
        from scripts.swarm import waits

        url = task.get("pr_url")
        pull = self.github()(url) if url else None
        if between_halves(task, pull):
            cast(Callable[..., Any], ledger.update_task)(
                who.swarm, task["id"], {"pr_url": "", "state": "claimed"}, if_state=("pr",)
            )
            task, pull = {**task, "pr_url": ""}, None
        target = named(live, task.get("pr_url"))
        if target and not (pull is not None and pull.state == "MERGED"):
            pull = None if waits.mutation_wait.RUN_URL.fullmatch(target) else self.github()(target)
            return {**task, "pr_url": target}, pull, named_ruling(pull), ""
        held: dict[str, Any] = (live or {}).get("on", {})
        dependency = cast(
            Callable[[dict[str, Any], dict[str, Any], dict[str, Any], Callable[..., Any]], bool], waits.dependency
        )(held, task, dependency_rows(ledger, who.swarm, held), self.github())
        return task, pull, *ruling(task, pull, live, dependency)

    def connect(self) -> "RedisStore":
        if self._connect:
            return self._connect()
        from scripts.swarm.store import connect

        return cast(Callable[[], "RedisStore"], connect)()

    def ledger(self) -> "LedgerClient":
        if self._ledger:
            return self._ledger()
        from scripts.swarm.ledger_client import LedgerClient

        return LedgerClient()

    def github(self) -> Callable[..., "PullRequest | None"]:
        if self._github:
            return self._github
        from scripts.swarm.ledger_events import view

        return view

    def now(self) -> int:
        if self._now:
            return self._now()
        import time

        return int(time.time() * 1000)


class Streak:
    """Stop blocks in a row: an outcome on the one progress signal since the last block starts the count again."""

    def __init__(self, store: "RedisStore", slug: str, name: str) -> None:
        self.store, self.slug, self.name = store, slug, name
        self.key = (
            f"{scope.readings(cast(Any, store.redis), slug, store.names.resolve(name))}:{slug}:stop-blocks:{name}"
        )

    def bump(self, now: int) -> int:
        from scripts.gates.progress import Progress

        held = json.loads(cast(Any, self.store.redis).get(self.key) or '{"count": 0, "at": 0}')
        outcome_at = Progress(cast(Any, self.store.redis), self.slug).read(self.name).outcome_at
        count = held["count"] if outcome_at <= held["at"] else 0
        cast(Any, self.store.redis).set(self.key, json.dumps({"count": count + 1, "at": now}))
        return count + 1

    def clear(self) -> None:
        cast(Any, self.store.redis).delete(self.key)


def block_task(store: "RedisStore", ledger: "LedgerClient", who: Who, note: str) -> None:
    from scripts.swarm.cli import block_agent
    from scripts.swarm.store import AgentRecord

    agents = cast(Callable[[str], list["AgentRecord"]], store.agents)(who.swarm)
    found = next((a for a in agents if a.name == who.name), None)
    cast(Callable[["RedisStore", str, "AgentRecord", str, "LedgerClient"], None], block_agent)(
        store, who.swarm, found or AgentRecord(who.name, lane_of(who.name), who.task), note, ledger
    )
