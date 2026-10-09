"""Restore saved conversations and retain failed resumes for an explicit decision."""

import json
import os
from dataclasses import asdict, dataclass, replace

from scripts import agent_choice
from scripts.handoff import transfers
from scripts.swarm.store import MASTER, SwarmError
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
    sources = f"the ledger {ledger}"
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
        from scripts.swarm.snapshot import ledger_path

        worktrees = json.loads(store.redis.get(store.key(slug, "restore-worktrees")) or "{}")
        worktree = store.config(slug).repo if agent.lane == MASTER else worktrees.get(name, "")
        outcome = _attempt(
            store,
            slug,
            agent,
            worktree,
            RestoreContext(runtime, now_ms, ledger_path(slug)),
            transfers.get(store, slug, previous["transfer"]),
        )
    else:
        raise SwarmError("Choose resume or fresh")
    store.put_restored(slug, [asdict(outcome) if r["name"] == name else r for r in store.restored(slug)])
    return outcome
