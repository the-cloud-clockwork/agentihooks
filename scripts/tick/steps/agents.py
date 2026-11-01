"""The agent steps of the tick pass: reap, outages, strays, failed panes, orphans and session upkeep."""

from collections.abc import Callable
from typing import Any

from scripts.tick import registry
from scripts.tick.steps import CallStep

__all__ = ["STEPS"]

Get = Callable[[str], Any]


def _reap(tick: Any, get: Get) -> Any:
    return tick.skip_refused(
        tick._reap, get("slug"), get("store"), get("ledger"), get("runtime"), get("rows"), get("now_ms")
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


def _strays(tick: Any, get: Get) -> Any:
    return tick.skip_refused(tick._strays, get("slug"), get("config"), get("store"), get("runtime"))


def _failed_panes(tick: Any, get: Get) -> Any:
    return tick.skip_refused(tick.failed_panes.sweep, get("slug"), get("store"), get("runtime"), get("now_ms"))


def _orphans(tick: Any, get: Get) -> Any:
    if get("config").engine == tick.EVENTS_ENGINE:
        return []
    return tick.skip_refused(tick._orphans, get("slug"), get("store"), get("ledger"), get("rows"))


def _conversations(tick: Any, get: Get) -> Any:
    return tick.skip_refused(tick._conversations, get("slug"), get("store"), get("runtime"))


def _session_models(tick: Any, get: Get) -> Any:
    return tick.skip_refused(tick._session_models, get("slug"), get("store"))


def _transfers(tick: Any, get: Get) -> Any:
    return tick.skip_refused(tick.transfers.observe, get("store"), get("slug"), get("runtime").live_names())


STEPS = (
    CallStep("reap", "agents", 10, _reap),
    CallStep("recover outages", "agents", 20, _recover_outages),
    CallStep("strays", "agents", 30, _strays),
    CallStep("failed panes", "agents", 40, _failed_panes),
    CallStep("orphans", "tasks", 0, _orphans),
    CallStep("conversations", "close", 100, _conversations),
    CallStep("session models", "close", 110, _session_models),
    CallStep("handoff transfers", "close", 120, _transfers),
)

for _step in STEPS:
    registry.register(_step)
