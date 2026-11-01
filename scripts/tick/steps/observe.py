"""The start and observation steps of the tick pass: config load, the stopped branch and the observation sweeps."""

from collections.abc import Callable
from functools import partial
from inspect import signature
from typing import Any

from scripts.tick import registry
from scripts.tick.steps import CallStep

RAISED = "raised"
PASS_KEYS = ("slug", "store", "ledger", "runtime", "now_ms", "box")


def _run(pick: Callable[[Any], Callable[..., Any]], tick: Any, get: Callable[[str], Any]) -> Any:
    """Run the tick step function ``pick`` finds with the pass state, skipping once a step held an exception."""
    box = get("box")
    if RAISED in box:
        return []
    try:
        call, values = pick(tick), {**{key: get(key) for key in PASS_KEYS}, **box}
        return call(**{name: values[name] for name in signature(call).parameters})
    except (Exception, SystemExit) as exc:
        box[RAISED] = exc
        return []


def raise_held(box: dict[str, Any]) -> None:
    """Raise the exception a step held, as the hand written pass let it escape."""
    if RAISED in box:
        raise box[RAISED]


STEPS = (
    CallStep("config load", "start", partial(_run, lambda tick: tick.step_config_load)),
    CallStep("stopped branch", "start", partial(_run, lambda tick: tick.step_stopped_branch)),
    CallStep("pane evidence", "observe", partial(_run, lambda tick: tick.step_pane_evidence)),
    CallStep("execution reconcile", "observe", partial(_run, lambda tick: tick.step_execution_reconcile)),
    CallStep("master move fail over", "observe", partial(_run, lambda tick: tick.step_master_move_fail_over)),
    CallStep("recover master", "observe", partial(_run, lambda tick: tick.step_recover_master)),
    CallStep("ledger state", "observe", partial(_run, lambda tick: tick.step_ledger_state)),
    CallStep("automated cleanup", "observe", partial(_run, lambda tick: tick.step_automated_cleanup)),
    CallStep("spawn due", "observe", partial(_run, lambda tick: tick.step_spawn_due)),
    CallStep("exits sweep", "observe", partial(_run, lambda tick: tick.step_exits_sweep)),
    CallStep("master start watch", "observe", partial(_run, lambda tick: tick.step_master_start_watch)),
    CallStep("launch checks", "observe", partial(_run, lambda tick: tick.step_launch_checks)),
    CallStep("track outages", "observe", partial(_run, lambda tick: tick.step_track_outages)),
    CallStep("verify", "observe", partial(_run, lambda tick: tick.step_verify)),
)

list(map(registry.register, STEPS))
