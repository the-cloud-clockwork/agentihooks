from collections.abc import Callable
from typing import Any

from scripts.tick.steps import CallStep
from scripts.tick.steps.tasks import held

Get = Callable[[str], Any]


def placing(call: Callable[[Any, Get], Any]) -> Callable[[Any, Get], Any]:
    def run(tick: Any, get: Get) -> Any:
        # ruleid: tick-step-takes-placement-lock-in-its-call
        with tick.PLACING:
            return call(tick, get)

    return run


def _quota_notice(tick: Any, get: Get) -> Any:
    # ruleid: tick-step-takes-placement-lock-in-its-call
    with tick.PLACING:
        return tick.skip_refused(tick.quota_notice.refresh, get("slug"))


def _rank_pass(tick: Any, get: Get) -> Any:
    # ok: tick-step-takes-placement-lock-in-its-call
    return tick.skip_refused(tick.rank_pass, get("slug"))


STEPS = (CallStep("rank pass", "placement", held(_rank_pass), placing=True),)


def _night_watch(tick: Any, get: Get) -> Any:
    def respawn() -> Any:
        # ok: tick-step-takes-placement-lock-in-its-call
        with tick.PLACING:
            return tick._master(get("slug"), forced=True)

    return tick.night_watch(respawn)


def _master_scale(tick: Any, get: Get) -> Any:
    def respawn() -> Any:
        # ruleid: tick-step-takes-placement-lock-in-its-call
        with tick.PLACING:
            return tick._master(get("slug"), forced=True)

    return respawn()
