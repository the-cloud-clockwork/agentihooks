"""The orders steps of the tick."""

from collections.abc import Callable
from typing import Any

from scripts.tick import registry
from scripts.tick.step import Lane
from scripts.tick.steps import CallStep


def _order_waiting(tick: Any, get: Callable[[str], Any]) -> list[str]:
    """Return the actions of the order waiting step, given the tick module and a reader of the pass state."""
    return []


registry.register(
    CallStep(
        "order waiting",
        "wrapper phases",
        _order_waiting,
        needs=frozenset(),
        reads=frozenset(),
        writes=frozenset(),
        lane=Lane("slow"),
        effects=frozenset({"coordination"}),
        feature="swarm-order-waiting-finding",
    )
)
