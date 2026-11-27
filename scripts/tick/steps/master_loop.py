"""The master_loop steps of the tick."""

from collections.abc import Callable
from typing import Any

from scripts.tick import registry
from scripts.tick.step import Lane
from scripts.tick.steps import CallStep


def _master_loop(tick: Any, get: Callable[[str], Any]) -> list[str]:
    """Return the actions of the master loop step, given the tick module and a reader of the pass state."""
    return []


registry.register(
    CallStep(
        "master loop",
        "wrapper events",
        _master_loop,
        needs=frozenset(),
        reads=frozenset(),
        writes=frozenset(),
        lane=Lane("tick"),
        effects=frozenset({"coordination", "inbox"}),
        feature="master-loop-seat-inbox",
    )
)
