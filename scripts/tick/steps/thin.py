"""The thin steps of the tick."""

from collections.abc import Callable
from typing import Any

from scripts.tick import registry
from scripts.tick.step import Lane
from scripts.tick.steps import CallStep


def _thin_task_pass(tick: Any, get: Callable[[str], Any]) -> list[str]:
    """Return the actions of the thin task pass step, given the tick module and a reader of the pass state."""
    return []


registry.register(
    CallStep(
        "thin task pass",
        "wrapper phases",
        _thin_task_pass,
        needs=frozenset(),
        reads=frozenset(),
        writes=frozenset(),
        lane=Lane("tick"),
        effects=frozenset({"inbox", "ledger"}),
        feature="tick-thin-task-pass",
    )
)
