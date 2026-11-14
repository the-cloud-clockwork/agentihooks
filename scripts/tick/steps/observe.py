"""The start and observation steps of the tick pass: config load, the stopped branch and the observation sweeps."""

# pyright: reportUnusedFunction=false

import json
from collections.abc import Callable
from dataclasses import replace
from functools import partial
from importlib import import_module
from inspect import signature
from types import MappingProxyType
from typing import Any

from scripts.swarm.ledger_client import LedgerOffline
from scripts.swarm.store import MASTER, SwarmConfig
from scripts.tick import registry
from scripts.tick.engine import SLOW
from scripts.tick.step import Lane
from scripts.tick.steps import CallStep, Left, budgets, pass_domains

reports: Any = import_module("scripts.hive.reports")
dead_resume: Any = import_module("scripts.swarm.dead_resume")
failed_panes: Any = import_module("scripts.swarm.failed_panes")
held_elsewhere: Any = import_module("scripts.swarm.held_elsewhere")
idle_state: Any = import_module("scripts.swarm.idle")
launch_check: Any = import_module("scripts.swarm.launch_check")
live_binding: Any = import_module("scripts.swarm.live_binding")
master_retire: Any = import_module("scripts.swarm.master_retire")
master_start: Any = import_module("scripts.swarm.master_start")
readiness: Any = import_module("scripts.swarm.readiness")
reaper: Any = import_module("scripts.swarm.reaper")
retire_watch: Any = import_module("scripts.swarm.retire_watch")

RAISED = "raised"
PASS_SETUP = "pass setup"
PASS_KEYS = ("slug", "store", "ledger", "runtime", "now_ms", "box")


def _run(call: Callable[..., Any], _module: Any, get: Callable[[str], Any]) -> Any:
    """Run the tick step function ``call`` with the pass state and the box values it declares, skipping once a step
    held an exception."""
    box = get("box")
    if RAISED in box:
        return []
    try:
        values = {key: get(key) for key in PASS_KEYS}
        return call(**{name: values[name] if name in values else box[name] for name in signature(call).parameters})
    except (Left, LedgerOffline):
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
        actions = automated_gc.run(
            slug, store, ledger, runtime, now_ms, partial(tick._collected, slug, store, ledger, rows)
        )
        retired = store.redis.get(store.key(slug, "master-retired-tasks")) is not None
        if not tick._woken(slug, config, store, ledger) and (not retired or tick.lifetime.sleeping(slug, store, rows)):
            box["finished"] = True
            return (
                actions
                + tick.skip_refused(tick._close_space, slug, config, store, runtime)
                + tick._bin_closed(slug, store, ledger, doc)
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

    return automated_gc.run(slug, store, ledger, runtime, now_ms, partial(tick._collected, slug, store, ledger, rows))


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
    return tick.skip_refused(tick._launch_checks, slug, store, ledger, runtime, rows, doc, now_ms)


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
    return tick.skip_refused(tick._verify, slug, store, ledger, runtime, rows, now_ms)


def step_spawn_hold_clear(slug: str, store: Any) -> list[str]:
    store.redis.delete(store.key(slug, tick.SPAWN_HOLD))
    return []


def step_pass_budget(slug: str, store: Any) -> list[str]:
    tick.engine.update_pass_budget(store, slug)
    return []


CONFIGURED = frozenset({"stopped branch"})
LISTED = budgets.OBSERVED
CONFIG = pass_domains("config")
ROWS = pass_domains("rows")


def _verify(slug: str, store: Any, ledger: Any, runtime: Any, rows: dict[str, Any], now_ms: int) -> list[str]:
    from scripts.hub.reconciler import admitted_agents, legacy_agents

    judged, legacy = launch_check.judged(store, slug), legacy_agents(store, slug)
    listed = legacy + admitted_agents(store, slug, legacy)
    agents, actions = [a for a in listed if a.name not in judged], list[str]()
    bound = [a for a in listed if a.name not in judged or live_binding.managed(a)]
    facts, live_names = runtime.bindings(bound), runtime.live_names()
    agents = [a for a in agents if a in legacy or a.name in facts]
    holders = held_elsewhere.holders(store)
    for agent in agents:
        if (
            agent.state == "awaiting-decision"
            or (agent.lane == MASTER and agent.state == "starting")
            or tick._acknowledging(slug, store, agent)
        ):
            continue
        task = rows.get(agent.task, {})
        if tick._ended(agent, rows):
            continue
        if (
            held_elsewhere.keep(slug, store, agent, holders, now_ms)
            or held_elsewhere.unseen(slug, store, agent, facts.get(agent.name, {}), now_ms)
            or dead_resume.skip_binding(agent, facts, live_names)
        ):
            continue
        agent, followed = tick._follow(slug, store, runtime, agent, facts.get(agent.name, {}).get("rebound"))
        actions.extend(followed)
        if agent is None:
            continue
        filled = live_binding.hosted_home(store, live_binding.fill(agent, facts.get(agent.name, {})))
        if filled != agent:
            store.put_agent(slug, filled)
            agent = filled
        differences = live_binding.record(store, slug, agent, facts.get(agent.name, {"process": False}), now_ms)
        if not differences:
            if agent.state == "retiring":
                store.put_agent(slug, replace(agent, state="working"))
            continue
        fields = ", ".join(differences)
        saved = live_binding.relaunch_assignment(agent, task, store.config(slug))
        if agent.lane == MASTER and "process" not in differences and not live_binding.complete(saved):
            actions.append(f"kept {agent.name} after mismatched {fields}: its relaunch assignment is incomplete")
            continue
        live = agent.name in facts and "process" not in differences
        if held := master_retire.hold(store, slug, agent, f"mismatched {fields}", live, now_ms):
            store.redis.hset(store.key(slug, "launch-assignments"), agent.task, json.dumps(saved))
            actions.append(held)
            continue
        gone = retire_watch.reported_gone(store, agent, now_ms)
        if not gone and not tick._retire_or_stop(slug, store, runtime, agent, "mismatch"):
            store.put_agent(slug, replace(agent, state="retiring"))
            actions.append(f"could not retire {agent.name} after mismatched {fields}, retrying next tick")
            continue
        store.redis.hset(store.key(slug, "launch-assignments"), agent.task, json.dumps(saved))
        dropped = tick._stop_hive(
            slug, store, agent, "mismatch", tick._drop(slug, store, ledger, rows, agent, refund=True)
        )
        actions.append(f"retired {agent.name} after mismatched {fields}" + dropped)
    return actions


def _launch_checks(
    slug: str, store: Any, ledger: Any, runtime: Any, rows: dict[str, Any], doc: dict[str, Any], now_ms: int
) -> list[str]:
    pending, actions = launch_check.pending(store, slug), list[str]()
    waiting = {name: entry for name, entry in pending.items() if entry.get("owner") != launch_check.HUB}
    if not waiting:
        return actions
    agents = [a for a in store.agents(slug) if a.name in waiting]
    for name in set(waiting) - {a.name for a in agents}:
        launch_check.forget(store, slug, name)
    confirmed = [launch_check.canary(store, slug, agent) for agent in agents]
    agents, canaries = [agent for agent, _ in confirmed], {agent.name: miss for agent, miss in confirmed}
    facts = runtime.bindings(agents)
    if any(launch_check.joined_at(agent, doc) is None for agent in agents):
        doc = tick.pass_state(ledger, slug)
    for agent in agents:
        found = launch_check.misses(
            store,
            slug,
            agent,
            facts.get(agent.name, {}),
            doc,
            launch_check.bundled(agent.profile),
            launch_check.expected(agent),
        )
        found.update(canaries[agent.name])
        if found and not launch_check.due(agent, found, now_ms):
            continue
        if found:
            miss = launch_check.Miss(agent, found, waiting[agent.name]["relaunch"])
            actions.append(tick._failed_launch(slug, store, ledger, runtime, rows, miss, now_ms))
            continue
        elapsed = (launch_check.joined_at(agent, doc) or now_ms) - launch_check.session_started_at(agent)
        launch_check.record(store, slug, agent, found, now_ms, elapsed)
        launch_check.forget(store, slug, agent.name)
        launch_check.clear_relaunched(store, slug, agent.task)
        tick._settle_admission(slug, runtime, agent)
        actions.append(f"{agent.name} passed its launch check in {elapsed // 1000} seconds")
    return actions


def _failed_hive_launch(
    slug: str, store: Any, ledger: Any, runtime: Any, rows: dict[str, Any], miss: Any, now_ms: int
) -> str:
    agent, found = miss.agent, miss.found
    if not tick._retire_launch(slug, store, runtime, agent, now_ms):
        return f"could not retire {agent.name} after its hive join failed, retrying next tick"
    launch_check.record(store, slug, agent, found, now_ms, now_ms - launch_check.session_started_at(agent))
    launch_check.forget(store, slug, agent.name)
    if agent.lane == MASTER:
        ledger.notify(slug, launch_check.told(found, "retired"))
    return f"retired {agent.name} after its hive join failed" + tick._drop(slug, store, ledger, rows, agent)


def _failed_launch(
    slug: str, store: Any, ledger: Any, runtime: Any, rows: dict[str, Any], miss: Any, now_ms: int
) -> str:
    agent, found = miss.agent, miss.found
    fields, elapsed = ", ".join(found), now_ms - launch_check.session_started_at(agent)
    if (
        idle_state.verdict(idle_state.IDLE, idle_state.heartbeat(store.redis, slug, agent.name), None, now_ms)
        == idle_state.WORKING
    ):
        launch_check.record(store, slug, agent, found, now_ms, elapsed, held=True)
        launch_check.forget(store, slug, agent.name)
        return f"{agent.name} failed its launch check on {fields}; reported only while working"
    hive = bool(agent.hive or reports.host(agent))
    if (
        hive
        and not set(found) <= launch_check.REPORT_ONLY
        and (not miss.relaunch or launch_check.relaunched(store, slug, agent.task))
    ):
        return tick._failed_hive_launch(slug, store, ledger, runtime, rows, miss, now_ms)
    if hive and set(found) <= launch_check.REPORT_ONLY:
        tick._settle_admission(slug, runtime, agent)
    if not miss.relaunch or set(found) <= launch_check.REPORT_ONLY:
        outcome, said = "report", f"{agent.name} failed its launch check on {fields}; reported only"
    elif launch_check.relaunched(store, slug, agent.task):
        outcome, said = "spent", f"{agent.name} failed its launch check on {fields}; its one relaunch is spent"
    elif held := master_retire.hold(
        store, slug, agent, f"its launch check failed on {fields}", agent.name in runtime.live_names(), now_ms
    ):
        return held
    elif not tick._retire_launch(slug, store, runtime, agent, now_ms):
        return f"could not retire {agent.name} after its launch check failed on {fields}, retrying next tick"
    else:
        outcome = "relaunch"
    launch_check.record(store, slug, agent, found, now_ms, elapsed, held=outcome == "spent")
    launch_check.forget(store, slug, agent.name)
    enforced = {field: values for field, values in found.items() if field not in launch_check.REPORT_ONLY}
    if agent.lane == MASTER and enforced:
        ledger.notify(slug, launch_check.told(enforced, outcome))
    if outcome != "relaunch":
        return said  # pyright: ignore[reportPossiblyUnboundVariable]
    launch_check.mark_relaunched(store, slug, agent.task)
    saved = live_binding.relaunch_assignment(agent, rows.get(agent.task, {}), store.config(slug))
    store.redis.hset(store.key(slug, "launch-assignments"), agent.task, json.dumps(saved))
    pending = master_start.read(store, slug)
    if agent.lane == MASTER and pending.get("name") == agent.name:
        master_start.save(store, slug, {**pending, "name": "", "retry": True, "at": now_ms})
    return f"retired {agent.name} after its launch check failed on {fields}" + tick._drop(
        slug, store, ledger, rows, agent
    )


def _retire_launch(slug: str, store: Any, runtime: Any, agent: Any, now_ms: int) -> bool:
    local = agent.runtime_backend == "local" and not reports.host(agent)
    exited = local and agent.lane != MASTER and agent.name not in runtime.live_names()
    kept = replace(agent, pane_id="") if exited else agent
    gone = retire_watch.reported_gone(store, agent, now_ms)
    if not gone and not runtime.retire(kept, homes=reaper.scratch_homes(slug, agent.task)):
        return False
    if exited:
        failed_panes.hold(store, slug, agent, now_ms, tick.EXITED_BEFORE_LIFE)
    return True


def _acknowledging(slug: str, store: Any, agent: Any) -> bool:
    if agent.lane == MASTER:
        return False
    raw = store.redis.hget(store.key(slug, "launches"), agent.name)
    return bool(raw) and json.loads(raw).get("state") in (tick.ACKNOWLEDGING, "launching")


STEPS = (
    CallStep(
        "config load",
        "start",
        partial(_run, step_config_load),
        frozenset({"command runner", "reboot pass"}),
        writes=CONFIG,
    ),
    CallStep(
        "stopped branch",
        "start",
        partial(_run, step_stopped_branch),
        frozenset({"config load"}),
        CONFIG,
        pass_domains("finished", "config"),
    ),
    CallStep("spawn hold clear", PASS_SETUP, partial(_run, step_spawn_hold_clear)),
    CallStep("pass budget", PASS_SETUP, partial(_run, step_pass_budget)),
    CallStep("pane evidence", "observe", partial(_run, step_pane_evidence), lane=SLOW),
    CallStep("execution reconcile", "observe", partial(_run, step_execution_reconcile)),
    CallStep("master move fail over", "observe", partial(_run, step_master_move_fail_over), CONFIGURED, CONFIG),
    CallStep("recover master", "observe", partial(_run, step_recover_master), CONFIGURED, CONFIG),
    CallStep("ledger state", "observe", partial(_run, step_ledger_state), writes=pass_domains("doc", "rows")),
    CallStep("automated cleanup", "observe", partial(_run, step_automated_cleanup), frozenset({"ledger state"}), ROWS),
    CallStep("exits sweep", "observe", partial(_run, step_exits_sweep)),
    CallStep("master start watch", "observe", partial(_run, step_master_start_watch), CONFIGURED, CONFIG),
    CallStep("launch checks", "observe", partial(_run, step_launch_checks), LISTED, pass_domains("rows", "doc")),
    CallStep("track outages", "observe", partial(_run, step_track_outages), LISTED, ROWS),
    CallStep("verify", "observe", partial(_run, step_verify), LISTED, ROWS),
)

list(map(registry.register, STEPS))

tick: Any = import_module("scripts.swarm.tick")


def _lab_capacity(tick: Any, get: Callable[[str], Any]) -> list[str]:
    """Read the CI lab capacity of the swarm's configured lab into its lab block and ship its rows to the metrics
    outbox; a swarm without a lab address reads nothing."""
    lab_capacity: Any = import_module("scripts.swarm.lab_capacity")
    metrics: Any = import_module("scripts.swarm.metrics")
    slug, store, now_ms = get("slug"), get("store"), get("now_ms")
    found = lab_capacity.capture(store, slug, store.config(slug).lab_url, now_ms)
    return metrics.record(lab_capacity.TABLE, found, now_ms) if found else []


registry.register(
    CallStep(
        "lab capacity",
        "observe",
        _lab_capacity,
        needs=frozenset(),
        reads=frozenset(),
        writes=frozenset(),
        lane=Lane("slow"),
        effects=frozenset({"metrics"}),
        feature="lab-capacity-reader",
    )
)
