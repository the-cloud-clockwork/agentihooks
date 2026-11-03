"""The task steps of the tick pass, run at their sites in today's order: task passes, then settle and close space."""

from collections.abc import Callable
from typing import Any

from scripts.tick import registry
from scripts.tick.steps import AGENTS, SWARM, TASKS, CallStep, Left

__all__ = ["CLOSE", "DRAINED", "RAISED", "REOPENS", "STEPS", "TASK_PASSES", "held", "raise_held"]

Get = Callable[[str], Any]
DRAINED = "drained resume"
REOPENS = "reopen passes"
TASK_PASSES = "task passes"
CLOSE = "close"
RAISED = "raised"


def held(call: Callable[[Any, Get], Any]) -> Callable[[Any, Get], Any]:
    """Wrap ``call`` to skip once a step held an exception and to hold the exception it raises."""

    def run(tick: Any, get: Get) -> Any:
        box = get("box")
        if RAISED in box:
            return []
        try:
            return call(tick, get)
        except Left:
            return []
        except (Exception, SystemExit) as exc:
            box[RAISED] = exc
            return []

    return run


def raise_held(box: dict[str, Any]) -> None:
    """Raise the exception a step held, as the hand written pass let it escape."""
    if RAISED in box:
        raise box[RAISED]


def _sleeping(tick: Any, get: Get) -> Any:
    get("box")["sleeping"] = tick.lifetime.sleeping(get("slug"), get("store"), get("rows"))
    return []


def _drained_resume(tick: Any, get: Get) -> Any:
    slug, store, rows, doc, config = get("slug"), get("store"), get("rows"), get("doc"), get("config")
    if get("box")["sleeping"] or config.state != "drained":
        return []
    if not any(tick._claimable(slug, store, rows, doc, lane) for lane in tick.LANES):
        return []
    get("box")["config"] = store.update(slug, state="running")
    return ["new tasks, running again"]


def _dev_red_reopen(tick: Any, get: Get) -> Any:
    from scripts.swarm import dev_red

    keys = ("slug", "config", "store", "ledger", "rows")
    return tick.skip_refused(dev_red.reopen_pass, *map(get, keys))


def _prerequisite_reopen(tick: Any, get: Get) -> Any:
    from scripts.swarm import prerequisite_wait

    keys = ("slug", "store", "ledger", "rows")
    return tick.skip_refused(prerequisite_wait.reopen_pass, *map(get, keys))


def _promotion_refresh(tick: Any, get: Get) -> Any:
    from scripts.swarm import promotion_watch

    keys = ("slug", "config", "store", "ledger", "doc", "now_ms")
    return tick.skip_refused(promotion_watch.refresh, *map(get, keys))


def _release_alert(tick: Any, get: Get) -> Any:
    from scripts.swarm import release_wait

    keys = ("slug", "config", "ledger", "doc")
    return tick.skip_refused(release_wait.alert_pass, *map(get, keys))


def _fleet_red_notice(tick: Any, get: Get) -> Any:
    from scripts.swarm import fleet_red

    keys = ("slug", "store", "ledger", "rows", "now_ms")
    return tick.skip_refused(fleet_red.notice_pass, *map(get, keys))


def _run_window(tick: Any, get: Get) -> Any:
    return tick.skip_refused(
        tick.run_window.step, get("slug"), get("config"), get("store"), get("ledger"), get("doc"), get("now_ms")
    )


def _verification_notices(tick: Any, get: Get) -> Any:
    return tick.skip_refused(tick.claim_hold.verification_notices, get("slug"), get("store"), get("doc"), get("now_ms"))


def _size_pass(tick: Any, get: Get) -> Any:
    return tick.skip_refused(tick.difficulty.size_pass, get("slug"), get("ledger"), get("doc"))


def _grouping_release(tick: Any, get: Get) -> Any:
    return tick.skip_refused(tick.grouping.release_pass, get("slug"), get("store"), get("ledger"), get("doc"))


def _dispatcher_group(tick: Any, get: Get) -> Any:
    return tick.skip_refused(
        tick.dispatcher.group, get("slug"), get("config"), get("store"), get("ledger"), get("doc"), get("now_ms")
    )


def _settle(tick: Any, get: Get) -> Any:
    return tick.skip_refused(
        tick._settle, get("slug"), get("config"), get("store"), get("ledger"), get("rows"), get("doc")
    )


def _close_space(tick: Any, get: Get) -> Any:
    return tick.skip_refused(tick._close_space, get("slug"), get("config"), get("store"), get("runtime"))


STEPS = (
    CallStep("sleeping", DRAINED, held(_sleeping)),
    CallStep("drained resume", DRAINED, held(_drained_resume), frozenset({"sleeping"})),
    CallStep("dev red reopen", REOPENS, held(_dev_red_reopen)),
    CallStep("prerequisite reopen", REOPENS, held(_prerequisite_reopen)),
    CallStep("promotion refresh", REOPENS, held(_promotion_refresh)),
    CallStep("release alert", REOPENS, held(_release_alert)),
    CallStep("fleet red notice", REOPENS, held(_fleet_red_notice)),
    CallStep("run window", TASK_PASSES, held(_run_window)),
    CallStep("verification notices", TASK_PASSES, held(_verification_notices)),
    CallStep("size pass", TASK_PASSES, held(_size_pass)),
    CallStep("grouping release", TASK_PASSES, held(_grouping_release)),
    CallStep("dispatcher group", TASK_PASSES, held(_dispatcher_group)),
    CallStep(
        "settle", CLOSE, held(_settle), frozenset({"spawn"}), frozenset({AGENTS, TASKS, SWARM}), frozenset({SWARM})
    ),
    CallStep("close space", CLOSE, held(_close_space)),
)

for _step in STEPS:
    registry.register(_step)
