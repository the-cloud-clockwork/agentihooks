"""The agent steps of the tick pass: reap, outages, strays, failed panes, orphans and session upkeep."""

from collections.abc import Callable
from importlib import import_module
from typing import Any

from scripts.swarm.store import AgentRecord
from scripts.tick import registry
from scripts.tick.steps import AGENTS, TASKS, CallStep, pass_domains

tick: Any = import_module("scripts.swarm.tick")

Get = Callable[[str], Any]


def _reap(slug: str, store: Any, ledger: Any, runtime: Any, rows: dict[str, dict[str, Any]], now_ms: int) -> list[str]:
    """Judge every legacy agent of the swarm, one task per pass worker, returning actions in agent order."""
    from scripts.hub.reconciler import legacy_agents

    live, rows = tick.timing.timed(tick._live_names, runtime), dict(rows)

    judging = tick._Reaping(slug, store, ledger, runtime, rows, live, now_ms)
    agents = list(tick.timing.timed(legacy_agents, store, slug))
    by_task: dict[str, list[int]] = {}
    for index, agent in enumerate(agents):
        by_task.setdefault(agent.task or agent.name, []).append(index)

    def one(indexes: list[int]) -> list[tuple[int, list[str]]]:
        """Return the actions judging each agent of one task took, in agent order."""
        judged: list[tuple[int, list[str]]] = []
        for index in indexes:
            with tick.timing.acting(agents[index].name):
                judged.append((index, tick._reap_agent(judging, agents[index])))
        return judged

    found = dict(pair for pairs in tick.pool.fan_out(one, by_task.values()) for pair in pairs)
    return [action for index in range(len(agents)) for action in found[index]]


def _strays(slug: Any, config: Any, store: Any, runtime: Any) -> Any:
    """Live agent processes still carrying a name this swarm issued and retired: every holder is left over."""
    actions: list[str]
    recorded, actions = {a.name for a in store.agents(slug)}, []
    for name in sorted(runtime.live_names() - recorded):
        found = tick.parse(name)
        if not found or found.code != config.code or not store.names.entry(name).get("retired_at"):
            continue
        if runtime.reap_name(name):
            actions.append(f"reaped stray {name}")
        else:
            actions.append(f"could not reap stray {name}, retrying next tick")
    return actions


def _orphans(slug: Any, store: Any, ledger: Any, rows: Any) -> Any:
    known = {a.name for a in store.agents(slug)}
    actions: list[str] = []
    for task_id, row in rows.items():
        if (
            row.get("state") in tick.ACTIVE
            and row.get("claimed_by") not in known
            and store.claimant(slug, task_id) is None
            and not tick.awaiting_merge(row)
        ):
            if tick._reopen(slug, ledger, rows, task_id):
                actions.append(f"task {task_id} had no agent, reopened")
    return actions


def _conversations(slug: str, store: Any, runtime: Any) -> None:
    """Record each paned agent's current conversation, fanned out over the pass workers."""
    local = runtime.conversations()
    found: dict[str, str] = {**(local or {}), **tick.reports.Reports(store.redis).conversations()}

    def one(agent: AgentRecord) -> None:
        """Pin and record the conversation the pane of ``agent`` reports."""
        if agent.state in {"finished", "awaiting-decision"} or not agent.pane_id:
            return
        pane = tick.reports.pane_key(agent)
        if pane not in found and (
            local is None or tick.reports.host(agent) or tick.resume.recovery_pending(store, slug, agent)
        ):
            return
        current = found.get(pane, "")
        if current:
            tick.context_recycle.pin_swarm(current, slug, store.config(slug).compact_limit or None)
        if current and current != agent.conversation_id:
            store.put_agent(slug, tick.replace(agent, conversation_id=current))
            store.names.note(agent.name, session_id=current)

    tick.pool.fan_out(one, store.agents(slug))


def _session_models(slug: Any, store: Any) -> Any:
    for agent in store.agents(slug):
        reported = tick.session_model.apply(agent, tick.session_model.get(store.redis, slug, agent.name))
        if reported != agent:
            store.put_agent(slug, reported)


def _reap_step(tick: Any, get: Get) -> Any:
    return tick.skip_refused(
        _reap, get("slug"), get("store"), get("ledger"), get("runtime"), get("rows"), get("now_ms")
    )


def _recover_outages(tick: Any, get: Get) -> Any:
    return tick.skip_refused(
        tick.resume.recover_outages,
        get("slug"),
        get("store"),
        get("ledger"),
        get("runtime"),
        get("rows"),
        get("now_ms"),
    )


def _strays_step(tick: Any, get: Get) -> Any:
    return tick.skip_refused(_strays, get("slug"), get("config"), get("store"), get("runtime"))


def _failed_panes(tick: Any, get: Get) -> Any:
    return tick.skip_refused(tick.failed_panes.sweep, get("slug"), get("store"), get("runtime"), get("now_ms"))


def _orphans_step(tick: Any, get: Get) -> Any:
    if get("config").engine == tick.EVENTS_ENGINE:
        return []
    return tick.skip_refused(_orphans, get("slug"), get("store"), get("ledger"), get("rows"))


def _conversations_step(tick: Any, get: Get) -> Any:
    return tick.skip_refused(_conversations, get("slug"), get("store"), get("runtime"))


def _session_models_step(tick: Any, get: Get) -> Any:
    return tick.skip_refused(_session_models, get("slug"), get("store"))


def _transfers(tick: Any, get: Get) -> Any:
    return tick.skip_refused(tick.transfers.observe, get("store"), get("slug"), get("runtime").live_names())


LISTED = frozenset({"spawn due"})
ROWS = pass_domains("rows")

STEPS = (
    CallStep("reap", "agents", _reap_step, LISTED, frozenset({AGENTS}) | ROWS, frozenset({AGENTS, TASKS})),
    CallStep("recover outages", "agents", _recover_outages, LISTED, ROWS),
    CallStep("strays", "agents", _strays_step, frozenset({"stopped branch"}), pass_domains("config")),
    CallStep("failed panes", "agents", _failed_panes),
    CallStep(
        "orphans",
        "tasks",
        _orphans_step,
        frozenset({"reap", "drained resume"}),
        frozenset({TASKS}) | pass_domains("config", "rows"),
        frozenset({TASKS}),
    ),
    CallStep("conversations", "close", _conversations_step),
    CallStep("session models", "close", _session_models_step),
    CallStep("handoff transfers", "close", _transfers),
)

for _step in STEPS:
    registry.register(_step)
