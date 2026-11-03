"""The start and observation steps of the tick pass: config load, the stopped branch and the observation sweeps."""

from collections.abc import Callable
from functools import partial
from importlib import import_module
from inspect import signature
from types import MappingProxyType
from typing import Any

from scripts.swarm.store import SwarmConfig
from scripts.tick import registry
from scripts.tick.steps import CallStep, Left, PassBox

tick: Any = import_module("scripts.swarm.tick")


def _bin_closed(slug: Any, store: Any, ledger: Any, doc: Any) -> Any:
    if doc.get("closed_at") and not store.agents(slug):
        tick.exits.close_swarm(tick.InboxStore(store.redis), slug)
        if ledger.bin_closed(slug, doc["closed_at"]):
            return ["the closed ledger moved to the bin"]
    return []


def _woken(slug: Any, config: Any, store: Any, ledger: Any) -> Any:
    inbox = tick.InboxStore(store.redis)
    waiting = inbox.pending_items(tick.seat_address(slug, tick.MASTER))
    if waiting and config.template == tick.priming.TEMPLATE and ledger.closed(slug):
        tick.priming.cancel_master_items(inbox, slug)
        return False
    return any(tick._operator_line(item) for item in waiting)


def _collected(slug: Any, store: Any, ledger: Any, rows: Any, agent: Any) -> Any:
    if agent.lane == tick.MASTER:
        store.redis.set(store.key(slug, "master-retired-tasks"), tick.json.dumps(list(rows)))
    return tick._drop(slug, store, ledger, rows, agent)


def _verify(slug: Any, store: Any, ledger: Any, runtime: Any, rows: Any, now_ms: Any) -> Any:
    from scripts.hub.reconciler import legacy_agents

    judged = tick.launch_check.judged(store, slug)
    actions: list[str]
    agents, actions = [a for a in legacy_agents(store, slug) if a.name not in judged], []
    facts, live_names = runtime.bindings(agents), runtime.live_names()
    for agent in agents:
        if (
            agent.state == "awaiting-decision"
            or (agent.lane == tick.MASTER and agent.state == "starting")
            or tick._acknowledging(slug, store, agent)
        ):
            continue
        task = rows.get(agent.task, {})
        if tick._ended(agent, rows):
            continue
        if tick.dead_resume.skip_binding(agent, facts, live_names):
            continue
        agent, followed = tick._follow(slug, store, runtime, agent, facts.get(agent.name, {}).get("rebound"))
        actions.extend(followed)
        if agent is None:
            continue
        filled = tick.live_binding.hosted_home(store, tick.live_binding.fill(agent, facts.get(agent.name, {})))
        if filled != agent:
            store.put_agent(slug, filled)
            agent = filled
        differences = tick.live_binding.record(store, slug, agent, facts.get(agent.name, {"process": False}), now_ms)
        if not differences:
            if agent.state == "retiring":
                store.put_agent(slug, tick.replace(agent, state="working"))
            continue
        fields = ", ".join(differences)
        saved = tick.live_binding.relaunch_assignment(agent, task, store.config(slug))
        if agent.lane == tick.MASTER and "process" not in differences and not tick.live_binding.complete(saved):
            actions.append(f"kept {agent.name} after mismatched {fields}: its relaunch assignment is incomplete")
            continue
        live = agent.name in facts and "process" not in differences
        if held := tick.master_retire.hold(store, slug, agent, f"mismatched {fields}", live, now_ms):
            store.redis.hset(store.key(slug, "launch-assignments"), agent.task, tick.json.dumps(saved))
            actions.append(held)
            continue
        if not runtime.retire(agent, homes=tick.reaper.scratch_homes(slug, agent.task)):
            store.put_agent(slug, tick.replace(agent, state="retiring"))
            actions.append(f"could not retire {agent.name} after mismatched {fields}, retrying next tick")
            continue
        store.redis.hset(store.key(slug, "launch-assignments"), agent.task, tick.json.dumps(saved))
        actions.append(
            f"retired {agent.name} after mismatched {fields}"
            + tick._drop(slug, store, ledger, rows, agent, refund=True)
        )
    return actions


def _launch_checks(
    slug: str, store: Any, ledger: Any, runtime: Any, rows: dict[str, Any], doc: dict[str, Any], now_ms: int
) -> list[str]:
    actions: list[str]
    waiting, actions = tick.launch_check.pending(store, slug), []
    if not waiting:
        return actions
    agents = [a for a in store.agents(slug) if a.name in waiting]
    for name in set(waiting) - {a.name for a in agents}:
        tick.launch_check.forget(store, slug, name)
    confirmed = [tick.launch_check.canary(store, slug, agent) for agent in agents]
    agents, canaries = [agent for agent, _ in confirmed], {agent.name: miss for agent, miss in confirmed}
    facts = runtime.bindings(agents)
    if any(tick.launch_check.joined_at(agent, doc) is None for agent in agents):
        doc = tick.pass_state(ledger, slug)
    for agent in agents:
        found = tick.launch_check.misses(
            store,
            slug,
            agent,
            facts.get(agent.name, {}),
            doc,
            tick.launch_check.bundled(agent.profile),
            tick.launch_check.expected(agent),
        )
        found.update(canaries[agent.name])
        if found and not tick.launch_check.due(agent, found, now_ms):
            continue
        if found:
            miss = tick.launch_check.Miss(agent, found, waiting[agent.name]["relaunch"])
            actions.append(tick._failed_launch(slug, store, ledger, runtime, rows, miss, now_ms))
            continue
        elapsed = (tick.launch_check.joined_at(agent, doc) or now_ms) - tick.launch_check.session_started_at(agent)
        tick.launch_check.record(store, slug, agent, found, now_ms, elapsed)
        tick.launch_check.forget(store, slug, agent.name)
        tick.launch_check.clear_relaunched(store, slug, agent.task)
        tick._settle_admission(slug, runtime, agent)
        actions.append(f"{agent.name} passed its launch check in {elapsed // 1000} seconds")
    return actions


RAISED = "raised"
PASS_SETUP = "pass setup"
PASS_KEYS = ("slug", "store", "ledger", "runtime", "now_ms", "box")


def _run(call: Callable[..., Any], _module: Any, get: Callable[[str], Any]) -> Any:
    """Run the tick step function ``call`` with the pass state, skipping once a step held an exception."""
    box = get("box")
    if RAISED in box:
        return []
    try:
        values = PassBox({**{key: get(key) for key in PASS_KEYS}, **box})
        return call(**{name: values[name] for name in signature(call).parameters})
    except Left:
        return []
    except (Exception, SystemExit) as exc:
        box[RAISED] = exc
        return []


def raise_held(box: dict[str, Any]) -> None:
    """Raise the exception a step held, as the hand written pass let it escape."""
    if RAISED in box:
        raise box[RAISED]


def step_config_load(slug: str, store: Any, box: dict[str, Any]) -> list[str]:
    box["config"] = store.ensure_code(slug)
    return []


def step_stopped_branch(
    slug: str,
    config: SwarmConfig,
    store: Any,
    ledger: Any,
    runtime: Any,
    now_ms: int,
    box: dict[str, Any],
) -> list[str]:
    automated_gc: Any = import_module("scripts.swarm.automated_gc")

    actions: list[str] = []
    if config.state == "stopped":
        doc = tick.timing.call(tick.pass_state, ledger, slug)
        rows = {t["id"]: t for t in doc["tasks"]}
        actions = automated_gc.run(slug, store, ledger, runtime, now_ms, partial(_collected, slug, store, ledger, rows))
        retired = store.redis.get(store.key(slug, "master-retired-tasks")) is not None
        if not _woken(slug, config, store, ledger) and (not retired or tick.lifetime.sleeping(slug, store, rows)):
            box["finished"] = True
            return (
                actions
                + tick.skip_refused(tick.task_steps._close_space, slug, config, store, runtime)
                + _bin_closed(slug, store, ledger, doc)
            )
        box["config"] = store.update(slug, state="paused")
        actions.append("the operator wrote on the ledger, paused to start the master")
    return actions


def step_pane_evidence(slug: str, store: Any, runtime: Any, now_ms: int) -> list[str]:
    return tick.skip_refused(tick.pane_evidence.sweep, slug, store, runtime, now_ms)


def step_execution_reconcile(slug: str, store: Any, runtime: Any, now_ms: int) -> list[str]:
    return tick.skip_refused(store.execution_registry.reconcile, slug, runtime, now_ms)


def step_master_move_fail_over(slug: str, config: SwarmConfig, store: Any, runtime: Any, now_ms: int) -> list[str]:
    return tick.skip_refused(tick.master_move.fail_over, slug, config, store, runtime, now_ms)


def step_recover_master(slug: str, config: SwarmConfig, store: Any, runtime: Any, now_ms: int) -> list[str]:
    return tick.skip_refused(tick._recover_master, slug, config, store, runtime, now_ms)


def step_ledger_state(slug: str, store: Any, ledger: Any, box: dict[str, Any]) -> list[str]:
    doc = tick.freeze.watched(slug, store, ledger, tick.timing.call(tick.pass_state, ledger, slug))
    box.update(doc=doc, rows=MappingProxyType({t["id"]: t for t in doc["tasks"]}))
    return []


def step_automated_cleanup(
    slug: str,
    store: Any,
    ledger: Any,
    runtime: Any,
    rows: dict[str, dict[str, Any]],
    now_ms: int,
) -> list[str]:
    automated_gc: Any = import_module("scripts.swarm.automated_gc")

    return automated_gc.run(slug, store, ledger, runtime, now_ms, partial(_collected, slug, store, ledger, rows))


def step_spawn_due(
    slug: str,
    config: SwarmConfig,
    store: Any,
    ledger: Any,
    runtime: Any,
    now_ms: int,
    box: dict[str, Any],
) -> list[str]:
    rows, doc = box["rows"], box["doc"]
    actions: list[str] = []
    due = store.key(slug, tick.SPAWN_DUE)
    if (
        config.state == "running"
        and config.engine != tick.EVENTS_ENGINE
        and not tick.lifetime.sleeping(slug, store, rows)
    ):
        if store.redis.exists(due):
            claimed = dict(rows)
            with tick.PLACING, tick.place._placement_pass(runtime):
                actions += tick.skip_refused(
                    tick.place._spawn, slug, config, store, ledger, runtime, claimed, doc, now_ms
                )
            box["rows"] = MappingProxyType(claimed)
        store.redis.set(due, now_ms)
    return actions


def step_exits_sweep(slug: str, store: Any, ledger: Any) -> list[str]:
    return tick.skip_dropped(
        tick.exits.sweep,
        tick.InboxStore(store.redis),
        slug,
        store,
        lambda: {t["id"]: t for t in tick.timing.call(tick.pass_state, ledger, slug)["tasks"]},
    )


def step_master_start_watch(
    slug: str, config: SwarmConfig, store: Any, ledger: Any, runtime: Any, now_ms: int
) -> list[str]:
    return tick.skip_refused(tick.master_start.observe, slug, config, store, ledger, runtime, now_ms)


def step_launch_checks(
    slug: str,
    store: Any,
    ledger: Any,
    runtime: Any,
    rows: dict[str, dict[str, Any]],
    doc: dict[str, Any],
    now_ms: int,
) -> list[str]:
    return tick.skip_refused(_launch_checks, slug, store, ledger, runtime, rows, doc, now_ms)


def step_track_outages(slug: str, store: Any, runtime: Any, rows: dict[str, dict[str, Any]], now_ms: int) -> list[str]:
    return tick.skip_refused(tick.resume.track_outages, slug, store, runtime, rows, now_ms) or []


def step_verify(
    slug: str,
    store: Any,
    ledger: Any,
    runtime: Any,
    rows: dict[str, dict[str, Any]],
    now_ms: int,
) -> list[str]:
    return tick.skip_refused(_verify, slug, store, ledger, runtime, rows, now_ms)


def step_spawn_hold_clear(slug: str, store: Any) -> list[str]:
    store.redis.delete(store.key(slug, tick.SPAWN_HOLD))
    return []


def step_pass_budget(slug: str, store: Any) -> list[str]:
    tick.engine.update_pass_budget(store, slug)
    return []


STEPS = (
    CallStep("config load", "start", partial(_run, step_config_load)),
    CallStep("stopped branch", "start", partial(_run, step_stopped_branch)),
    CallStep("spawn hold clear", PASS_SETUP, partial(_run, step_spawn_hold_clear)),
    CallStep("pass budget", PASS_SETUP, partial(_run, step_pass_budget)),
    CallStep("pane evidence", "observe", partial(_run, step_pane_evidence)),
    CallStep("execution reconcile", "observe", partial(_run, step_execution_reconcile)),
    CallStep("master move fail over", "observe", partial(_run, step_master_move_fail_over)),
    CallStep("recover master", "observe", partial(_run, step_recover_master)),
    CallStep("ledger state", "observe", partial(_run, step_ledger_state)),
    CallStep("automated cleanup", "observe", partial(_run, step_automated_cleanup)),
    CallStep("spawn due", "observe", partial(_run, step_spawn_due)),
    CallStep("exits sweep", "observe", partial(_run, step_exits_sweep)),
    CallStep("master start watch", "observe", partial(_run, step_master_start_watch)),
    CallStep("launch checks", "observe", partial(_run, step_launch_checks)),
    CallStep("track outages", "observe", partial(_run, step_track_outages)),
    CallStep("verify", "observe", partial(_run, step_verify)),
)

list(map(registry.register, STEPS))
