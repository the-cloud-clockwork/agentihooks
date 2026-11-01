"""The start and observation steps of the tick pass: config load, the stopped branch and the observation sweeps."""

from collections.abc import Callable
from functools import partial
from typing import Any

from scripts.tick import registry
from scripts.tick.steps import CallStep

__all__ = ["RAISED", "STEPS", "raise_held"]

Get = Callable[[str], Any]
Box = dict[str, Any]
RAISED = "raised"


def _held(call: Callable[[Any, Get, Box], Any]) -> Callable[[Any, Get], Any]:
    """Wrap ``call`` to skip once a step held an exception and to hold the exception it raises."""

    def run(tick: Any, get: Get) -> Any:
        box = get("box")
        if RAISED in box:
            return []
        try:
            return call(tick, get, box)
        except (Exception, SystemExit) as exc:
            box[RAISED] = exc
            return []

    return run


def raise_held(box: Box) -> None:
    """Raise the exception a step held, as the hand written pass let it escape."""
    if RAISED in box:
        raise box[RAISED]


def _cleanup(tick: Any, get: Get, rows: dict[str, Any]) -> Any:
    from scripts.swarm import automated_gc

    slug, store, ledger = get("slug"), get("store"), get("ledger")
    collected = partial(tick._collected, slug, store, ledger, rows)
    return automated_gc.run(slug, store, ledger, get("runtime"), get("now_ms"), collected)


def _config_load(tick: Any, get: Get, box: Box) -> Any:
    box["config"] = get("store").ensure_code(get("slug"))
    return []


def _stopped_branch(tick: Any, get: Get, box: Box) -> Any:
    slug, config, store, ledger, runtime = get("slug"), box["config"], get("store"), get("ledger"), get("runtime")
    if config.state != "stopped":
        return []
    doc = tick.timing.call(ledger.state, slug)
    rows = {t["id"]: t for t in doc["tasks"]}
    actions = _cleanup(tick, get, rows)
    retired = store.redis.get(store.key(slug, "master-retired-tasks")) is not None
    if not tick._woken(slug, config, store, ledger) and (not retired or tick.lifetime.sleeping(slug, store, rows)):
        closed = tick.skip_refused(tick._close_space, slug, config, store, runtime)
        box["finished"] = closed + tick._bin_closed(slug, store, ledger, doc)
        return actions
    box["config"] = store.update(slug, state="paused")
    actions.append("the operator wrote on the ledger, paused to start the master")
    return actions


def _pane_evidence(tick: Any, get: Get, box: Box) -> Any:
    return tick.skip_refused(tick.pane_evidence.sweep, get("slug"), get("store"), get("runtime"), get("now_ms"))


def _execution_reconcile(tick: Any, get: Get, box: Box) -> Any:
    return tick.skip_refused(get("store").execution_registry.reconcile, get("slug"), get("runtime"), get("now_ms"))


def _fail_over(tick: Any, get: Get, box: Box) -> Any:
    args = (get("slug"), box["config"], get("store"), get("runtime"), get("now_ms"))
    return tick.skip_refused(tick.master_move.fail_over, *args)


def _recover_master(tick: Any, get: Get, box: Box) -> Any:
    return tick.skip_refused(
        tick._recover_master, get("slug"), box["config"], get("store"), get("runtime"), get("now_ms")
    )


def _ledger_state(tick: Any, get: Get, box: Box) -> Any:
    slug, ledger = get("slug"), get("ledger")
    doc = tick.freeze.watched(slug, get("store"), ledger, tick.timing.call(ledger.state, slug))
    box["doc"], box["rows"] = doc, {t["id"]: t for t in doc["tasks"]}
    return []


def _automated_cleanup(tick: Any, get: Get, box: Box) -> Any:
    return _cleanup(tick, get, box["rows"])


def _spawn_due(tick: Any, get: Get, box: Box) -> Any:
    slug, config, store, ledger, runtime, rows = (
        get("slug"),
        box["config"],
        get("store"),
        get("ledger"),
        get("runtime"),
        box["rows"],
    )
    actions = []
    due = store.key(slug, tick.SPAWN_DUE)
    if (
        config.state == "running"
        and config.engine != tick.EVENTS_ENGINE
        and not tick.lifetime.sleeping(slug, store, rows)
    ):
        if store.redis.exists(due):
            with tick.PLACING, tick._placement_pass(runtime):
                actions += tick.skip_refused(
                    tick._spawn, slug, config, store, ledger, runtime, rows, box["doc"], get("now_ms")
                )
        store.redis.set(due, get("now_ms"))
    return actions


def _exits_sweep(tick: Any, get: Get, box: Box) -> Any:
    slug, store, ledger = get("slug"), get("store"), get("ledger")
    return tick.skip_dropped(
        tick.exits.sweep,
        tick.InboxStore(store.redis),
        slug,
        store,
        lambda: {t["id"]: t for t in tick.timing.call(ledger.state, slug)["tasks"]},
    )


def _master_start(tick: Any, get: Get, box: Box) -> Any:
    args = (get("slug"), box["config"], get("store"), get("ledger"), get("runtime"), get("now_ms"))
    return tick.skip_refused(tick.master_start.observe, *args)


def _launch_checks(tick: Any, get: Get, box: Box) -> Any:
    args = (get("slug"), get("store"), get("ledger"), get("runtime"), box["rows"], box["doc"], get("now_ms"))
    return tick.skip_refused(tick._launch_checks, *args)


def _track_outages(tick: Any, get: Get, box: Box) -> Any:
    args = (get("slug"), get("store"), get("runtime"), box["rows"], get("now_ms"))
    return tick.skip_refused(tick.resume.track_outages, *args) or []


def _verify(tick: Any, get: Get, box: Box) -> Any:
    args = (get("slug"), get("store"), get("ledger"), get("runtime"), box["rows"], get("now_ms"))
    return tick.skip_refused(tick._verify, *args)


STEPS = (
    CallStep("config load", "start", 10, _held(_config_load)),
    CallStep("stopped branch", "start", 20, _held(_stopped_branch)),
    CallStep("pane evidence", "observe", 10, _held(_pane_evidence)),
    CallStep("execution reconcile", "observe", 20, _held(_execution_reconcile)),
    CallStep("master move fail over", "observe", 30, _held(_fail_over)),
    CallStep("recover master", "observe", 40, _held(_recover_master)),
    CallStep("ledger state", "observe", 50, _held(_ledger_state)),
    CallStep("automated cleanup", "observe", 60, _held(_automated_cleanup)),
    CallStep("spawn due", "observe", 70, _held(_spawn_due)),
    CallStep("exits sweep", "observe", 80, _held(_exits_sweep)),
    CallStep("master start watch", "observe", 90, _held(_master_start)),
    CallStep("launch checks", "observe", 100, _held(_launch_checks)),
    CallStep("track outages", "observe", 110, _held(_track_outages)),
    CallStep("verify", "observe", 120, _held(_verify)),
)

for _step in STEPS:
    registry.register(_step)
