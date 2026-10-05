"""Restore reopens each saved agent in its own conversation when it can, else marks it finished to start fresh."""

import os
from dataclasses import asdict, dataclass, replace

from scripts import agent_choice
from scripts.swarm.store import MASTER
from scripts.swarm_ledger import ledger_workspace

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


def reopen(store, slug, worktrees, runtime, now_ms, ledger, has_quota=account_quota, exists=os.path.isdir):
    """Resume every unfinished agent it can; the rest are marked finished. Records and returns each outcome."""
    config, outcomes = store.ensure_code(slug), []
    for agent in store.agents(slug):
        if agent.state == "finished":
            continue
        worktree = config.repo if agent.lane == MASTER else worktrees.get(agent.name, "")
        why = blocker(agent, worktree, exists, has_quota)
        if not why:
            try:
                placed = runtime.resume(config, agent, restored_text(slug, agent, ledger))
            except Exception as exc:
                why = f"resume failed to start: {exc}"
            else:
                account = placed.account or agent.account
                back = replace(agent, pane_id=placed.pane_id, account=account, started_at=now_ms, idle_ticks=0)
                store.put_agent(slug, replace(back, state="working"))
        if why:
            store.put_agent(slug, replace(agent, state="finished"))
        result = FRESH if why else RESUMED
        outcomes.append(
            Outcome(agent.name, agent.lane, agent.task, result, why or REOPENED, agent.conversation_id, now_ms)
        )
    store.put_restored(slug, [asdict(o) for o in outcomes])
    return outcomes
