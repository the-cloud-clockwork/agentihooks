"""The start and observation steps of the tick pass: config load, the stopped branch and the observation sweeps."""

from collections.abc import Callable
from functools import partial
from inspect import signature
from typing import Any

from scripts.swarm.store import RedisStore, SwarmConfig
from scripts.tick import registry
from scripts.tick.steps import CallStep

RAISED = "raised"
PASS_KEYS = ("slug", "store", "ledger", "runtime", "now_ms", "box")


def _run(call: Callable[..., Any], tick: Any, get: Callable[[str], Any]) -> Any:
    """Run the tick step function ``call`` with the tick module and the pass state, skipping once a step held an exception."""
    box = get("box")
    if RAISED in box:
        return []
    try:
        values = {"tick": tick, **{key: get(key) for key in PASS_KEYS}, **box}
        return call(**{name: values[name] for name in signature(call).parameters})
    except (Exception, SystemExit) as exc:
        box[RAISED] = exc
        return []


def raise_held(box: dict[str, Any]) -> None:
    """Raise the exception a step held, as the hand written pass let it escape."""
    if RAISED in box:
        raise box[RAISED]


def step_config_load(tick: Any, slug: str, store: RedisStore, box: dict[str, Any]) -> list[str]:
    box["config"] = store.ensure_code(slug)
    return []


def step_stopped_branch(
    tick: Any,
    slug: str,
    config: SwarmConfig,
    store: RedisStore,
    ledger: Any,
    runtime: Any,
    now_ms: int,
    box: dict[str, Any],
) -> list[str]:
    from scripts.swarm import automated_gc

    actions: list[str] = []
    if config.state == "stopped":
        doc = tick.timing.call(ledger.state, slug)
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


def step_pane_evidence(tick: Any, slug: str, store: RedisStore, runtime: Any, now_ms: int) -> list[str]:
    return tick.skip_refused(tick.pane_evidence.sweep, slug, store, runtime, now_ms)


def step_execution_reconcile(tick: Any, slug: str, store: RedisStore, runtime: Any, now_ms: int) -> list[str]:
    return tick.skip_refused(store.execution_registry.reconcile, slug, runtime, now_ms)


def step_master_move_fail_over(
    tick: Any, slug: str, config: SwarmConfig, store: RedisStore, runtime: Any, now_ms: int
) -> list[str]:
    return tick.skip_refused(tick.master_move.fail_over, slug, config, store, runtime, now_ms)


def step_recover_master(
    tick: Any, slug: str, config: SwarmConfig, store: RedisStore, runtime: Any, now_ms: int
) -> list[str]:
    return tick.skip_refused(tick._recover_master, slug, config, store, runtime, now_ms)


def step_ledger_state(tick: Any, slug: str, store: RedisStore, ledger: Any, box: dict[str, Any]) -> list[str]:
    doc = tick.freeze.watched(slug, store, ledger, tick.timing.call(ledger.state, slug))
    rows = {t["id"]: t for t in doc["tasks"]}
    box.update(doc=doc, rows=rows)
    return []


def step_automated_cleanup(
    tick: Any,
    slug: str,
    store: RedisStore,
    ledger: Any,
    runtime: Any,
    rows: dict[str, dict[str, Any]],
    now_ms: int,
) -> list[str]:
    from scripts.swarm import automated_gc

    return automated_gc.run(slug, store, ledger, runtime, now_ms, partial(tick._collected, slug, store, ledger, rows))


def step_spawn_due(
    tick: Any,
    slug: str,
    config: SwarmConfig,
    store: RedisStore,
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
            with tick.PLACING, tick._placement_pass(runtime):
                actions += tick.skip_refused(tick._spawn, slug, config, store, ledger, runtime, rows, doc, now_ms)
        store.redis.set(due, now_ms)
    return actions


def step_exits_sweep(tick: Any, slug: str, store: RedisStore, ledger: Any) -> list[str]:
    return tick.skip_dropped(
        tick.exits.sweep,
        tick.InboxStore(store.redis),
        slug,
        store,
        lambda: {t["id"]: t for t in tick.timing.call(ledger.state, slug)["tasks"]},
    )


def step_master_start_watch(
    tick: Any, slug: str, config: SwarmConfig, store: RedisStore, ledger: Any, runtime: Any, now_ms: int
) -> list[str]:
    return tick.skip_refused(tick.master_start.observe, slug, config, store, ledger, runtime, now_ms)


def step_launch_checks(
    tick: Any,
    slug: str,
    store: RedisStore,
    ledger: Any,
    runtime: Any,
    rows: dict[str, dict[str, Any]],
    doc: dict[str, Any],
    now_ms: int,
) -> list[str]:
    return tick.skip_refused(tick._launch_checks, slug, store, ledger, runtime, rows, doc, now_ms)


def step_track_outages(
    tick: Any, slug: str, store: RedisStore, runtime: Any, rows: dict[str, dict[str, Any]], now_ms: int
) -> list[str]:
    return tick.skip_refused(tick.resume.track_outages, slug, store, runtime, rows, now_ms) or []


def step_verify(
    tick: Any,
    slug: str,
    store: RedisStore,
    ledger: Any,
    runtime: Any,
    rows: dict[str, dict[str, Any]],
    now_ms: int,
) -> list[str]:
    return tick.skip_refused(tick._verify, slug, store, ledger, runtime, rows, now_ms)


STEPS = (
    CallStep("config load", "start", 10, partial(_run, step_config_load)),
    CallStep("stopped branch", "start", 20, partial(_run, step_stopped_branch)),
    CallStep("pane evidence", "observe", 10, partial(_run, step_pane_evidence)),
    CallStep("execution reconcile", "observe", 20, partial(_run, step_execution_reconcile)),
    CallStep("master move fail over", "observe", 30, partial(_run, step_master_move_fail_over)),
    CallStep("recover master", "observe", 40, partial(_run, step_recover_master)),
    CallStep("ledger state", "observe", 50, partial(_run, step_ledger_state)),
    CallStep("automated cleanup", "observe", 60, partial(_run, step_automated_cleanup)),
    CallStep("spawn due", "observe", 70, partial(_run, step_spawn_due)),
    CallStep("exits sweep", "observe", 80, partial(_run, step_exits_sweep)),
    CallStep("master start watch", "observe", 90, partial(_run, step_master_start_watch)),
    CallStep("launch checks", "observe", 100, partial(_run, step_launch_checks)),
    CallStep("track outages", "observe", 110, partial(_run, step_track_outages)),
    CallStep("verify", "observe", 120, partial(_run, step_verify)),
)

list(map(registry.register, STEPS))
