"""The start and observation steps of the tick pass: config load, the stopped branch and the observation sweeps."""

from collections.abc import Callable
from typing import Any

from scripts.tick import registry
from scripts.tick.steps import CallStep

RAISED = "raised"
KEYS = ("slug", "store", "ledger", "runtime", "now_ms", "box")


def _held(name: str) -> Callable[[Any, Callable[[str], Any]], Any]:
    """Run the tick's step function ``name`` with the pass state, skipping once a step held an exception."""

    def run(tick: Any, get: Callable[[str], Any]) -> Any:
        box = get("box")
        if RAISED in box:
            return []
        try:
            return getattr(tick, name)(**{key: get(key) for key in KEYS}, **box)
        except (Exception, SystemExit) as exc:
            box[RAISED] = exc
            return []

    return run


def raise_held(box: dict[str, Any]) -> None:
    """Raise the exception a step held, as the hand written pass let it escape."""
    if RAISED in box:
        raise box[RAISED]


STEPS = (
    CallStep("config load", "start", 10, _held("_step_config_load")),
    CallStep("stopped branch", "start", 20, _held("_step_stopped_branch")),
    CallStep("pane evidence", "observe", 10, _held("_step_pane_evidence")),
    CallStep("execution reconcile", "observe", 20, _held("_step_execution_reconcile")),
    CallStep("master move fail over", "observe", 30, _held("_step_master_move_fail_over")),
    CallStep("recover master", "observe", 40, _held("_step_recover_master")),
    CallStep("ledger state", "observe", 50, _held("_step_ledger_state")),
    CallStep("automated cleanup", "observe", 60, _held("_step_automated_cleanup")),
    CallStep("spawn due", "observe", 70, _held("_step_spawn_due")),
    CallStep("exits sweep", "observe", 80, _held("_step_exits_sweep")),
    CallStep("master start watch", "observe", 90, _held("_step_master_start_watch")),
    CallStep("launch checks", "observe", 100, _held("_step_launch_checks")),
    CallStep("track outages", "observe", 110, _held("_step_track_outages")),
    CallStep("verify", "observe", 120, _held("_step_verify")),
)

for _step in STEPS:
    registry.register(_step)
