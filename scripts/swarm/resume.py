"""Restore saved conversations and retain failed resumes for an explicit decision."""

import json
import os
from dataclasses import asdict, dataclass, replace

from scripts import agent_choice
from scripts.handoff import transfers
from scripts.swarm import idle, outage, snapshot
from scripts.swarm.store import MASTER, AgentRecord, RedisStore, SwarmError
from scripts.swarm_ledger import ledger_workspace

AWAITING = "awaiting-decision"
RESUMED, FRESH = "resumed", "fresh"
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


def account_quota(harness, account):
    return agent_choice.account_has_quota(harness, account, dict(os.environ))


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
        folder = ledger_workspace.folder(slug, agent.task)
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
    elif choice == "resume":
        from scripts.swarm.snapshot import ledger_source

        worktrees = json.loads(store.redis.get(store.key(slug, "restore-worktrees")) or "{}")
        worktree = store.config(slug).repo if agent.lane == MASTER else worktrees.get(name, "")
        outcome = _attempt(
            store,
            slug,
            agent,
            worktree,
            RestoreContext(runtime, now_ms, ledger_source(slug)),
            transfers.get(store, slug, previous["transfer"]),
        )
    else:
        raise SwarmError("Choose resume or fresh")
    store.put_restored(slug, [asdict(outcome) if r["name"] == name else r for r in store.restored(slug)])
    return outcome


RECOVERY = "outage-resumes"
RECOVERY_ATTEMPTS = 3


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
        return not agent.execution_id
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


def track_outages(slug: str, store: RedisStore, runtime: object, rows: dict, now_ms: int) -> None:
    records = outage.read(slug)
    key = store.key(slug, RECOVERY)
    live = runtime.live_names()
    for agent in store.agents(slug):
        previous = _recovery_record(store, slug, agent)
        if not _recovery_owner(store, slug, agent, rows, now_ms):
            if previous:
                store.redis.hdel(key, agent.name)
            continue
        episodes = sorted(
            [record["kind"], record["started_at"]]
            for record in records.values()
            if record["ended_at"] is None or record["ended_at"] >= agent.started_at
        )
        if not episodes:
            continue
        identity = _recovery_identity(store, slug, agent)
        if previous.get("episodes") == episodes and previous.get("identity") == identity:
            continue
        observed = runtime.observe(agent)
        if agent.name in live and observed.state != idle.IDLE and not observed.provider_error:
            continue
        store.redis.hset(
            key,
            agent.name,
            json.dumps({"identity": identity, "episodes": episodes, "attempts": 0, "state": "pending", "last_at": 0}),
        )


def recovery_pending(store: RedisStore, slug: str, agent: AgentRecord) -> bool:
    record = _recovery_record(store, slug, agent)
    return (
        record.get("state") == "pending"
        and record["attempts"] < RECOVERY_ATTEMPTS
        and record["identity"] == _recovery_identity(store, slug, agent)
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
    folder = task.get("workspace") or str(ledger_workspace.folder(slug, agent.task))
    return (
        f"Service is back after the outage. As {agent.name}, re-read your task folder {folder} "
        f"(steering.md, progress.md, proof.md), then run agentihooks msg inbox and continue your own task {agent.task}. "
        "Keep your existing claim; do not claim new work."
    )


def recover_outages(
    slug: str, store: RedisStore, ledger: object, runtime: object, rows: dict, now_ms: int
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
        elif agent.name in live and (observed.typed or observed.state not in {idle.IDLE, idle.WORKING}):
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
    from scripts.swarm.tick import placed_record, primed

    worktree = snapshot.worktrees(store.config(slug).repo, [agent.name]).get(agent.name, "")
    why = blocker(agent, worktree)
    handoff = store.handoff(slug, agent.task) or transfers.last_handoff(store, slug, agent.task)
    if not _recovery_current(store, slug, agent, context):
        return "task ownership changed"
    try:
        if not why:
            placed = context.runtime.resume(store.config(slug), agent, text)
            resumed = replace(
                agent,
                pane_id=placed.pane_id,
                started_at=context.at,
                idle_ticks=0,
                profile_decision=placed.profile_decision or agent.profile_decision,
            )
        else:
            transfer = transfers.record(store, slug, agent, "outage", handoff, context.at)
            transfers.attach(store, slug, agent)
            task = primed(store, slug, agent.seat, {**task, "handoff": handoff, "transfer": transfer["id"]})
            if not _recovery_current(store, slug, agent, context):
                return "task ownership changed"
            placed = context.runtime.spawn(store.config(slug), agent.lane, agent.name, task)
            resumed = replace(placed_record(agent, placed), conversation_id="", started_at=context.at, idle_ticks=0)
    except Exception as error:
        return str(error) or type(error).__name__
    store.put_agent(slug, resumed)
    return ""
