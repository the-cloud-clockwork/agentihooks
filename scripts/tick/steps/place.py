"""The placement steps of the tick pass, run at their sites in today's order: notices, acks, ranking and preempt."""

# pyright: reportUnusedFunction=false, reportUnusedClass=false

import json
from collections.abc import Callable
from contextvars import ContextVar
from dataclasses import replace
from functools import partial
from importlib import import_module
from typing import Any

from scripts.swarm import lost_runner, quota_notice, time_left
from scripts.swarm.naming import seat_label
from scripts.tick import engine, registry
from scripts.tick.adapters.calls import (
    AdmissionCalls,
    LocalMetrics,
    MetricsCalls,
)
from scripts.tick.engine import SLOW
from scripts.tick.steps import AGENTS, TASKS, CallStep, pass_domains
from scripts.tick.steps.tasks import RANK, WriteStep, held, placing, rank_writes

Who: Any = import_module("scripts.gates").Who
modes: Any = import_module("scripts.gates.modes")
claim_cap: Any = import_module("scripts.gates.claims")
gate_log: Any = import_module("scripts.gates.log")
preempt: Any = import_module("scripts.swarm.preempt")

__all__ = ["ACKS", "NOTICES", "PLACED", "PLACEMENT", "PREEMPT_NEEDS", "STEPS"]

Get = Callable[[str], Any]
NOTICES = "placement notices"
ACKS = "placement acks"
PLACEMENT = "placement"
PREEMPT_NEEDS = frozenset({"reap", "drained resume", "recover outages", "prerequisite reopen"})
RESUMED = frozenset({"drained resume"})
CONFIG_DOC = pass_domains("config", "doc")
TASK_STATE = pass_domains("config", "rows", "doc")
PLACED = frozenset({AGENTS, TASKS})


PASS_METRICS: ContextVar[MetricsCalls | None] = ContextVar("spawn_pass_metrics", default=None)


def _quota_notice(tick: Any, get: Get) -> Any:
    keys = ("slug", "config", "store", "ledger", "runtime", "now_ms")
    return tick.skip_refused(quota_notice.refresh, *map(get, keys))


def _lost_runner(tick: Any, get: Get) -> Any:
    keys = ("slug", "config", "store", "now_ms")
    return tick.skip_refused(lost_runner.sweep, *map(get, keys))  # pyright: ignore[reportUnknownMemberType]


def _time_left(tick: Any, get: Get) -> Any:
    keys = ("slug", "store", "ledger", "runtime", "config", "doc")
    return tick.skip_refused(time_left.refresh, *map(get, keys))  # pyright: ignore[reportUnknownMemberType]


def _codex_hook_order(tick: Any, get: Get) -> Any:
    if not get("sleeping"):
        return tick.skip_refused(tick._codex_hook_order)
    return []


def _rank_pass(tick: Any, get: Get) -> Any:
    keys = ("slug", "config", "store", "ledger", "doc", "now_ms")
    return tick.skip_refused(rank_writes, tick, *map(get, keys))


def _bottleneck_remedy(tick: Any, get: Get) -> Any:
    return tick.skip_refused(
        tick.bottleneck_remedy.step, get("slug"), get("store"), get("ledger"), get("doc"), get("now_ms")
    )


def _preempt(tick: Any, get: Get) -> Any:
    config, store = get("config"), get("store")
    if config.state == "running" and config.engine != tick.EVENTS_ENGINE and tick.panic.state(store) is None:
        placement = partial(tick.preempt.probe, get("runtime"))
        seats = tick.preempt.Seats(
            get("rows"), tick._ended, tick._current_owner, tick.ready_in_order, placement, tick.claim_view
        )
        return tick.skip_refused(tick.preempt.run, get("slug"), config, store, get("doc"), get("now_ms"), seats)
    return []


def _admission(runtime: Any) -> Any:
    from scripts.swarm_v2.admission import PendingAdmission

    gate = getattr(runtime, "admission", None)
    return gate if isinstance(gate, (PendingAdmission, AdmissionCalls)) else None


def _settle_admission(slug: str, runtime: Any, agent: Any) -> None:
    gate = tick._admission(runtime)
    if gate is not None and (reservation := gate.held(slug, agent.task)):
        gate.activate(slug, agent.task, reservation)


def _host_full(slug: str, store: Any, now_ms: int, in_flight: int = 0) -> str:
    capacity: Any = import_module("scripts.swarm.capacity")

    host: dict[str, Any] = capacity.read(store, slug).get("host") or {}
    if host.get("room") is None:
        return ""
    recent = tick.host_spent(store, host["granted_at"], now_ms) + in_flight
    if recent < host["room"]:
        return ""
    return f"host {host['limit']} room {host['room']}, {recent} spawned since it was granted: {host['reason']}"


def host_spent(store: Any, since_ms: int, now_ms: int) -> int:
    return store.redis.zcount(tick.HOST_SPENDS, since_ms - tick.HOST_START_LAG_MS, now_ms)


def _spend_host(store: Any, name: str, now_ms: int) -> None:
    store.redis.zadd(tick.HOST_SPENDS, {name: now_ms})
    store.redis.zremrangebyrank(tick.HOST_SPENDS, 0, -tick.HOST_SPENDS_KEPT - 1)


def _spend_local(store: Any, agent: Any, now_ms: int) -> None:
    from scripts.swarm.commands import hive_id

    if not agent.hive or agent.hive == hive_id():
        tick._spend_host(store, agent.name, now_ms)


def _release_hive_launch(slug: str, store: Any, record: Any) -> None:
    from scripts.hive import launch

    launch.release(store, slug, next((a for a in store.agents(slug) if a.name == record.name), record))


def _hold(slug: str, store: Any, text: str) -> str:
    store.redis.set(store.key(slug, tick.SPAWN_HOLD), text)
    return text


def spawn_holds(store: Any, slug: str) -> list[str]:
    held = store.redis.get(store.key(slug, tick.SPAWN_HOLD))
    return [hold for hold in (held, engine.pass_budget_hold(store, slug)) if hold]


def _lives_spent(slug: str, store: Any, ledger: Any, rows: dict[str, Any], task: dict[str, Any]) -> str:
    try:
        return tick._claim_cap(slug, store, ledger, rows, task)
    except Exception as exc:
        who = Who(name="swarm", task=task["id"])
        row = gate_log.Row.of(claim_cap.GATE.name, "fail-open", who, reason=f"{type(exc).__name__}: {exc}")
        _ports().log(slug, [row])
        return ""


def _ports() -> MetricsCalls | LocalMetrics:
    """The spawn step's metrics and processes ports, else this host's gate log and process table."""
    return PASS_METRICS.get() or LocalMetrics(gate_log.append, import_module("hooks.proc").processes)


def _claim_cap(slug: str, store: Any, ledger: Any, rows: dict[str, Any], task: dict[str, Any]) -> str:
    lives, mode = store.claims(slug, task["id"]), modes.configured(claim_cap.GATE, store.config(slug).gates)
    if lives < claim_cap.CAP or mode == "off":
        return ""
    last = (store.handoff_envelope(slug, task["id"]) or {}).get("reason") or "none"  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
    failure = claim_cap.launch_failure(lives, store.launches(slug), task["id"])
    reason = claim_cap.refusal(lives, last, failure, slug, task["id"])
    kind = "observe" if mode == "observe" else "deny"
    _ports().log(slug, [gate_log.Row.of(claim_cap.GATE.name, kind, Who(name="swarm", task=task["id"]), reason=reason)])
    if kind == "observe":
        return ""
    live = ledger.update_task(slug, task["id"], {"state": "blocked"}, if_state=("open",))
    rows[task["id"]].update(live)
    if live["state"] != "blocked":
        return ""
    store.reset_claims(slug, task["id"])
    ledger.comment(slug, task["id"], reason, by="swarm")
    return f"blocked {task['id']}: {reason}"


def primed(store: Any, slug: str, seat: str, task: dict[str, Any]) -> dict[str, Any]:
    priming_trace: Any = import_module("scripts.swarm.priming_trace")

    memory = {"recaps": store.memory.recaps(seat), "learned": store.memory.learned(seat)}
    envelope = task.get("handoff_envelope", store.handoff_envelope(slug, task["id"]))
    previous = store.execution_occupants(slug).get(seat)
    task = {
        **task,
        "seat": seat,
        "previous_execution_id": previous.execution_id if previous else "",
        "culture": store.culture.get(slug),
        "handoff_envelope": envelope,
        **memory,
    }
    saved = store.redis.hget(store.key(slug, "launch-assignments"), task["id"])
    if saved:
        task["launch_assignment"] = json.loads(saved)
    return priming_trace.withhold(slug, task)


def _free_seat(slug: str, lane: str, taken: set[str], preferred: str) -> str:
    if preferred and preferred not in taken:
        return preferred
    return next(
        seat for k in range(1, len(taken) + 2) if (seat := tick.seat_address(slug, seat_label(lane, k))) not in taken
    )


def placed_record(record: Any, placed: Any) -> Any:
    record = placed.execution or record
    return replace(
        record,
        pane_id=placed.pane_id,
        conversation_id=placed.conversation_id or record.conversation_id,
        harness=placed.harness,
        account=placed.account,
        model=placed.model,
        effort=placed.effort,
        placement=placed.placement,
        profile=placed.profile,
        model_source=placed.model_source,
        model_confidence=placed.model_confidence,
        profile_decision=placed.profile_decision,
        choice=placed.choice,
        launched_at=placed.launched_at or record.started_at,
        overlays=placed.overlays,
        launch_timings=placed.launch_timings,
        route_kind=placed.route_kind,
        transport=placed.transport or record.transport,
        remote_control=placed.remote_control or record.remote_control,
        desired_route_kind=record.desired_route_kind
        if placed.desired_route_kind is None
        else placed.desired_route_kind,
        state="working",
    )


STEPS = (
    CallStep("quota notice", NOTICES, held(placing(_quota_notice)), RESUMED, CONFIG_DOC),
    CallStep("lost runner", NOTICES, held(_lost_runner), RESUMED, pass_domains("config"), lane=SLOW),
    CallStep("time left", NOTICES, held(_time_left), RESUMED, CONFIG_DOC, lane=SLOW),
    CallStep(
        "codex hook order", ACKS, held(placing(_codex_hook_order)), frozenset({"sleeping"}), pass_domains("sleeping")
    ),
    WriteStep(
        "rank pass", PLACEMENT, held(placing(_rank_pass)), writes=frozenset({RANK}), needs=RESUMED, reads=CONFIG_DOC
    ),
    CallStep(
        "bottleneck remedy", PLACEMENT, held(_bottleneck_remedy), frozenset({"ledger state"}), pass_domains("doc")
    ),
    CallStep(
        "preempt",
        PLACEMENT,
        held(placing(_preempt)),
        PREEMPT_NEEDS,
        frozenset({AGENTS}) | TASK_STATE,
        frozenset({AGENTS}),
    ),
)

for _step in STEPS:
    registry.register(_step)
registry.require("scripts.tick.steps.report")

tick: Any = import_module("scripts.swarm.tick")
