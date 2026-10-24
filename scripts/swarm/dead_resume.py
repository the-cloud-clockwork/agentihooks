from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING

from hooks.context import context_recycle
from scripts.swarm.store import MASTER, AgentRecord, RedisStore

if TYPE_CHECKING:
    from scripts.swarm.tick import Ledger, Runtime


def skip_binding(agent: AgentRecord, facts: dict, live: set[str]) -> bool:
    from scripts.hive import reports

    return agent.state != "retiring" and (
        agent.name not in facts
        or (
            facts[agent.name].get("process") is False
            and agent.name not in live
            and agent.runtime_backend == "local"
            and not reports.host(agent)
        )
    )


def _owns(store, slug, agent, ledger):
    from scripts.swarm.tick import LEASE_MS

    if agent not in store.agents(slug):
        return False
    if agent.seat and store.seats.occupant(agent.seat).occupant != agent.name:
        return False
    if agent.lane == MASTER:
        return True
    task = ledger.task(slug, agent.task) or {}
    return (
        task.get("state") in {"claimed", "pr"}
        and task.get("claimed_by") == agent.name
        and store.ensure_claim(slug, agent, LEASE_MS)
    )


def _reason(store, slug, agent):
    if store.handoff(slug, agent.task):
        return "a handoff was submitted"
    if not agent.conversation_id:
        return "resume failed because no conversation id was recorded"
    used = context_recycle._used(agent.conversation_id)
    limit = store.config(slug).compact_limit or context_recycle.COMPACT_LIMIT
    if used is not None and used >= limit * 1000:
        return "the conversation reached its compact limit"
    return ""


FRESH_NOTICE = "Starting a fresh agent after {name} ended because {reason}."


def reopen(
    slug: str,
    store: RedisStore,
    ledger: Ledger,
    runtime: Runtime,
    agent: AgentRecord,
    at: int,
    notice: str = FRESH_NOTICE,
) -> bool:
    config = store.config(slug)
    if config.state in {"stopped", "stopping"} or not _owns(store, slug, agent, ledger):
        return False
    reason = _reason(store, slug, agent)
    if not reason:
        try:
            placed = runtime.resume(
                config,
                agent,
                "Your pane ended unexpectedly. Continue your task in this conversation and seat. "
                "Read your task progress and proof and handle your inbox before continuing.",
            )
        except Exception as error:
            reason = f"resume failed: {error}"
        else:
            if not _owns(store, slug, agent, ledger):
                runtime.retire(replace(agent, pane_id=placed.pane_id))
                return False
            store.put_agent(
                slug,
                replace(
                    agent,
                    pane_id=placed.pane_id,
                    launched_at=placed.launched_at or at,
                    idle_ticks=0,
                    account=placed.account or agent.account,
                    model=placed.model or agent.model,
                    effort=placed.effort or agent.effort,
                    model_source=placed.model_source or agent.model_source,
                    profile=placed.profile or agent.profile,
                    route_kind=placed.route_kind,
                    remote_control=placed.remote_control,
                    desired_route_kind=agent.desired_route_kind
                    if placed.desired_route_kind is None
                    else placed.desired_route_kind,
                    profile_decision={**agent.profile_decision, **placed.profile_decision},
                    launch_timings=placed.launch_timings,
                ),
            )
            store.names.note(agent.name, session_id=agent.conversation_id)
            ledger.notify(slug, f"Resumed {agent.name} in its own conversation and seat after its pane ended.")
            return True
    if notice:
        ledger.notify(slug, notice.format(name=agent.name, reason=reason))
    return False
