"""Restore saved conversations and retain failed resumes for an explicit decision."""

import json
import os
from dataclasses import asdict, dataclass, replace
from typing import TYPE_CHECKING

from scripts import agent_choice
from scripts.handoff import transfers
from scripts.swarm import deliberate_exit, idle, outage, profile_choice
from scripts.swarm.store import MASTER, AgentRecord, RedisStore, SwarmError
from scripts.swarm_ledger import ledger_workspace

if TYPE_CHECKING:
    from scripts.swarm.tick import Ledger, Runtime

AWAITING = "awaiting-decision"
RESUMED, FRESH = "resumed", "fresh"
KUBERNETES = "kubernetes"
RECOVERY_REQUESTS = "recovery-requests"
NATIVE, CONTEXT = "native", "context"
CHOICES = ("resume", NATIVE, CONTEXT, FRESH)
UNCHOSEN = "Choose native, context, fresh or resume"
LOCAL_CONTEXT = (
    "a local swarm keeps no checkpoint to continue from; a fresh start primes the successor with the seat handoff"
)
REOPENED = "own conversation reopened"
RESTORED = (
    "The swarm {slug} was restored from a snapshot and reopened this conversation of yours as {name}, on task {task}. "
    "Time passed and other agents may have moved the work. Before acting, re-read {sources}, then continue from what "
    "they say."
)


@dataclass(frozen=True)
class Outcome:
    name: str
    lane: str
    task: str
    outcome: str
    reason: str
    conversation_id: str = ""
    at: int = 0
    transfer: str = ""
    decided: bool = False
    awaited_at: int = 0


def account_quota(harness, account):
    return agent_choice.account_has_quota(harness, account, dict(os.environ))


def quota_ignored(harness, account):
    return True


def blocker(agent, worktree, exists=os.path.isdir, has_quota=account_quota):
    """Why the agent cannot reopen its own conversation; empty when it can."""
    if not agent.conversation_id:
        return "no conversation id"
    if not worktree:
        return "no worktree"
    if not exists(worktree):
        return "worktree gone"
    if agent.account and has_quota(agent.harness, agent.account) is False:
        return f"account {agent.account} out of quota"
    return ""


def restored_text(slug, agent, ledger):
    sources = f"the ledger, printed by {ledger}"
    if agent.lane != MASTER:
        folder = ledger_workspace.named(slug, agent)
        sources = f"your task folder {folder} (steering.md, progress.md, proof.md) and {sources}"
    return RESTORED.format(slug=slug, name=agent.name, task=agent.task, sources=sources)


def reopen(store, slug, worktrees, runtime, now_ms, ledger, has_quota=account_quota):
    context = RestoreContext(runtime, now_ms, ledger, has_quota)
    store.redis.set(store.key(slug, "restore-worktrees"), json.dumps(worktrees))
    outcomes = []
    for agent in store.agents(slug):
        if agent.state == "finished":
            continue
        worktree = store.config(slug).repo if agent.lane == MASTER else worktrees.get(agent.name, "")
        text = store.handoff(slug, agent.task) or transfers.last_handoff(store, slug, agent.task)
        transfer = transfers.record(store, slug, agent, "restore", text, now_ms)
        outcomes.append(_attempt(store, slug, agent, worktree, context, transfer))
    store.put_restored(slug, [asdict(o) for o in outcomes])
    return outcomes


@dataclass(frozen=True)
class RestoreContext:
    runtime: object
    at: int
    ledger: object
    has_quota: object = account_quota


def _attempt(store, slug, agent, worktree, context, transfer):
    if agent.seat:
        if store.seats.occupant(agent.seat).occupant != agent.name:
            store.seats.occupy(agent.seat, agent.name, context.at)
        transfers.attach(store, slug, agent)
    why = blocker(agent, worktree, has_quota=context.has_quota)
    if not why:
        try:
            placed = context.runtime.resume(
                store.ensure_code(slug),
                agent,
                restored_text(slug, agent, context.ledger) + "\n" + transfers.priming(slug, transfer),
            )
        except Exception as exc:
            why = f"resume failed to start: {exc}"
        else:
            agent = replace(
                agent,
                pane_id=placed.pane_id,
                account=placed.account or agent.account,
                model=placed.model or agent.model,
                effort=placed.effort or agent.effort,
                model_source=placed.model_source,
                model_confidence=placed.model_confidence,
                started_at=context.at,
                idle_ticks=0,
                state="working",
            )
    if why:
        agent = replace(agent, state=AWAITING)
    store.put_agent(slug, agent)
    live = context.runtime.live_names() if context.runtime else set()
    transfers.observe(store, slug, live)
    return Outcome(
        agent.name,
        agent.lane,
        agent.task,
        AWAITING if why else RESUMED,
        why or REOPENED,
        agent.conversation_id,
        context.at,
        transfer["id"],
    )


def decide(store, slug: str, name: str, choice: str, runtime, now_ms: int, ledger) -> Outcome:
    agent = next((a for a in store.agents(slug) if a.name == name), None)
    if agent is None or agent.state != AWAITING:
        raise SwarmError("This agent is not awaiting a restore decision")
    if agent.execution_id and agent.runtime_backend == KUBERNETES:
        return requested(store, slug, agent, choice, now_ms)
    if choice == CONTEXT:
        raise SwarmError(LOCAL_CONTEXT)
    previous = next(r for r in store.restored(slug) if r["name"] == name)
    if choice == "fresh":
        transfers.fresh(store, slug, previous["transfer"], now_ms)
        store.put_agent(slug, replace(agent, state="finished"))
        if agent.lane != MASTER:
            ledger.update_task(slug, agent.task, {"state": "open", "claimed_by": ""})
        outcome = Outcome(
            name,
            agent.lane,
            agent.task,
            FRESH,
            "Fresh start explicitly chosen",
            at=now_ms,
            transfer=previous["transfer"],
        )
    elif choice in ("resume", NATIVE):
        from scripts.swarm.snapshot import ledger_source

        worktrees = json.loads(store.redis.get(store.key(slug, "restore-worktrees")) or "{}")
        worktree = store.config(slug).repo if agent.lane == MASTER else worktrees.get(name, "")
        lost = blocker(agent, worktree, has_quota=quota_ignored)
        if choice == NATIVE and lost:
            raise SwarmError(lost)
        outcome = _attempt(
            store,
            slug,
            agent,
            worktree,
            RestoreContext(runtime, now_ms, ledger_source(slug)),
            transfers.get(store, slug, previous["transfer"]),
        )
    else:
        raise SwarmError(UNCHOSEN)
    outcome = replace(
        outcome, decided=outcome.outcome != AWAITING, awaited_at=previous.get("awaited_at") or previous["at"]
    )
    store.put_restored(slug, [asdict(outcome) if r["name"] == name else r for r in store.restored(slug)])
    return outcome


def requested(store, slug: str, agent, choice: str, now_ms: int) -> Outcome:
    """A Pod attempt's choice is only requested here; the controller applies it under its lease on its next pass,
    and a repeated request for the same attempt replaces the pending one instead of queueing a second."""
    if choice not in CHOICES:
        raise SwarmError(UNCHOSEN)
    store.redis.hset(store.key(slug, RECOVERY_REQUESTS), agent.execution_id, choice)
    reason = f"Recovery requested: {choice}"
    return Outcome(agent.name, agent.lane, agent.task, AWAITING, reason, agent.conversation_id, now_ms)


RECOVERY = "outage-resumes"
RECOVERY_ATTEMPTS = 3
STUCK_HANDOFF = """## Intent
Continue task {task}; the swarm retired {name} while its turns kept failing on a provider error.

## Done
unknown: {name} left no handoff document; its task folder records its steps and evidence.

## Stopped at
Its pane showed this provider error: {error}

## Decisions and promises
unknown: none recorded outside the task folder and the ledger.

## Next
Read the task folder {folder} (steering.md, progress.md, proof.md) and the ledger task {task}, then continue the task.

## Read first
{folder}/progress.md

<!-- handoff complete -->
"""


def _recovery_record(store, slug, agent):
    raw = store.redis.hget(store.key(slug, RECOVERY), agent.name)
    return json.loads(raw) if raw else {}


def _recovery_owner(store, slug, agent, rows, now_ms):
    task = rows.get(agent.task, {})
    if agent.lane == MASTER or agent.state != "working":
        return False
    if task.get("done") or task.get("state") not in {"claimed", "pr"} or task.get("claimed_by") != agent.name:
        return False
    if store.claimant(slug, agent.task) != agent.name:
        return False
    if agent.seat and store.seats.occupant(agent.seat).occupant != agent.name:
        return False
    raw = store.redis.get(store.key(slug, "task-authority", agent.task))
    if not raw:
        return not agent.execution_id or agent.runtime_backend == "local"
    claim = json.loads(raw)
    return (
        claim["state"] == "active"
        and claim["holder"] == agent.name
        and claim["execution_id"] == agent.execution_id
        and claim["execution_generation"] == agent.generation
        and claim["lease_deadline_ms"] > now_ms
    )


def _recovery_identity(store, slug, agent):
    raw = store.redis.get(store.key(slug, "task-authority", agent.task))
    generation = json.loads(raw)["generation"] if raw else 0
    return [agent.task, agent.seat, agent.execution_id, agent.generation, generation]


def track_outages(slug: str, store: RedisStore, runtime: "Runtime", rows: dict, now_ms: int) -> None:
    records = outage.read(slug)
    key = store.key(slug, RECOVERY)
    live = runtime.live_names()
    for agent in store.agents(slug):
        previous = _recovery_record(store, slug, agent)
        if not _recovery_owner(store, slug, agent, rows, now_ms):
            if previous:
                store.redis.hdel(key, agent.name)
            continue
        heartbeat = idle.heartbeat(store.redis, slug, agent.name)
        last_work = max(agent.started_at, (heartbeat or {}).get("at", 0))
        if previous.get("state") == "pending":
            last_work = agent.started_at
        episodes = sorted(
            [record["kind"], record["started_at"]]
            for record in records.values()
            if record["ended_at"] is None or record["ended_at"] >= last_work
        )
        if not episodes:
            if previous.get("state") == "pending":
                previous["state"] = "done"
                store.redis.hset(key, agent.name, json.dumps(previous))
            continue
        identity = _recovery_identity(store, slug, agent)
        if previous.get("episodes") == episodes and previous.get("identity") == identity:
            continue
        observed = runtime.observe(agent)
        state = idle.verdict(
            observed.state,
            heartbeat,
            idle.wait(store.redis, slug, agent.name),
            now_ms,
        )
        stalled = agent.name not in live or bool(observed.provider_error) or state == idle.IDLE
        if not stalled and any(record["ended_at"] is None for record in records.values()):
            continue
        store.redis.hset(
            key,
            agent.name,
            json.dumps(
                {
                    "identity": identity,
                    "episodes": episodes,
                    "attempts": 0,
                    "state": "pending" if stalled else "done",
                    "last_at": 0,
                }
            ),
        )


def recovery_pending(store: RedisStore, slug: str, agent: AgentRecord) -> bool:
    record = _recovery_record(store, slug, agent)
    return (
        record.get("state") == "pending"
        and record["attempts"] < RECOVERY_ATTEMPTS
        and record["identity"] == _recovery_identity(store, slug, agent)
        and not deliberate_exit.recorded(store, slug, agent.name)
    )


def recovery_exhausted(store: RedisStore, slug: str, agent: AgentRecord) -> bool:
    record = _recovery_record(store, slug, agent)
    return record.get("state") == "exhausted" and record["identity"] == _recovery_identity(store, slug, agent)


def _recovery_current(store, slug, agent, context):
    rows = {task["id"]: task for task in context.ledger.state(slug)["tasks"]}
    record = _recovery_record(store, slug, agent)
    return (
        record.get("state") == "pending"
        and record["identity"] == _recovery_identity(store, slug, agent)
        and _recovery_owner(store, slug, agent, rows, context.at)
    )


def _recovery_text(slug, agent, task):
    folder = ledger_workspace.named(slug, agent, task.get("workspace"))
    return (
        f"Service is back after the outage. As {agent.name}, re-read your task folder {folder} "
        f"(steering.md, progress.md, proof.md), then run agentihooks msg inbox and continue your own task {agent.task}. "
        "Keep your existing claim; do not claim new work."
    )


def recover_outages(
    slug: str, store: RedisStore, ledger: "Ledger", runtime: "Runtime", rows: dict, now_ms: int
) -> list[str]:
    if outage.open_records(slug):
        return []
    actions, live = [], runtime.live_names()
    context = RestoreContext(runtime, now_ms, ledger)
    key = store.key(slug, RECOVERY)
    for agent in store.agents(slug):
        if not recovery_pending(store, slug, agent) or not _recovery_owner(store, slug, agent, rows, now_ms):
            continue
        record = _recovery_record(store, slug, agent)
        observed = runtime.observe(agent)
        if record["last_at"] == now_ms or not _recovery_current(store, slug, agent, context):
            continue
        if agent.name in live and not observed.provider_error and observed.state == idle.WORKING:
            record["state"] = "done"
        elif agent.name in live and (
            observed.typed
            or observed.prompt_title
            or (observed.state not in {idle.IDLE, idle.WORKING} and not observed.provider_error)
        ):
            continue
        else:
            record["attempts"] += 1
            record["last_at"] = now_ms
            store.redis.hset(key, agent.name, json.dumps(record))
            text = _recovery_text(slug, agent, rows[agent.task])
            if agent.name in live:
                why = runtime.nudge(agent, text)
                if not why and not observed.provider_error:
                    record["state"] = "done"
            else:
                why = _recover_process(store, slug, agent, rows[agent.task], context, text)
                if not why:
                    record["state"] = "done"
            if record["state"] == "pending" and record["attempts"] >= RECOVERY_ATTEMPTS:
                record["state"] = "exhausted"
            actions.append(f"outage resume for {agent.name}" + (f" refused: {why}" if why else ""))
        store.redis.hset(key, agent.name, json.dumps(record))
    return actions


def _recover_process(store, slug, agent, task, context, text):
    from scripts.swarm import snapshot
    from scripts.swarm.tick import SpawnError

    worktree = snapshot.worktrees(store.config(slug).repo, [agent.name]).get(agent.name, "")
    why = blocker(agent, worktree)
    if not _recovery_current(store, slug, agent, context):
        return "task ownership changed"
    try:
        if not why:
            try:
                placed = context.runtime.resume(store.config(slug), agent, text)
            except SpawnError as error:
                if error.status != "unsupported":
                    raise
            else:
                store.put_agent(
                    slug,
                    replace(
                        agent,
                        pane_id=placed.pane_id,
                        started_at=context.at,
                        idle_ticks=0,
                        account=placed.account or agent.account,
                        model=placed.model or agent.model,
                        effort=placed.effort or agent.effort,
                        model_source=placed.model_source or agent.model_source,
                        profile_decision={**agent.profile_decision, **placed.profile_decision},
                    ),
                )
                return ""
        return _restart_from_handoff(store, slug, agent, task, context)
    except Exception as error:
        return str(error) or type(error).__name__


def _restart_from_handoff(store, slug, agent, task, context):
    from scripts.swarm.tick import SpawnError, placed_record, primed

    if not _recovery_current(store, slug, agent, context):
        return "task ownership changed"
    handoff = recovery_text(store, slug, agent.task)
    transfers.record(store, slug, agent, "outage", handoff, context.at)
    transfer = transfers.attach(store, slug, agent)
    task = primed(store, slug, agent.seat, {**task, "handoff": handoff, "transfer": transfer})
    if not _recovery_current(store, slug, agent, context):
        return "task ownership changed"
    config = store.config(slug)
    try:
        placed = context.runtime.spawn(config, agent.lane, agent.name, task)
    except SpawnError as error:
        if error.status != "unsupported":
            raise
        placed = context.runtime.spawn(config, agent.lane, agent.name, {**task, profile_choice.FRESH: True})
    store.put_agent(
        slug,
        replace(
            placed_record(agent, placed), conversation_id=placed.conversation_id, started_at=context.at, idle_ticks=0
        ),
    )
    return ""


def recovery_text(store: RedisStore, slug: str, task: str) -> str:
    return store.handoff(slug, task) or transfers.last_handoff(store, slug, task)


def free_seat(store: RedisStore, slug: str, agent: AgentRecord, now_ms: int) -> dict:
    store.redis.hdel(store.key(slug, RECOVERY), agent.name)
    error = store.redis.get(store.key(slug, outage.PANE, agent.name))
    folder = ledger_workspace.folder(slug, agent.task)
    handoff = recovery_text(store, slug, agent.task) or STUCK_HANDOFF.format(
        task=agent.task, name=agent.name, error=error, folder=folder
    )
    return transfers.record(store, slug, agent, "outage", handoff, now_ms, agent.harness)
