"""The workflows steps of the tick."""

from collections.abc import Callable
from typing import Any

from scripts.tick import registry
from scripts.tick.step import Lane
from scripts.tick.steps import CallStep


def _workflow_runner(tick: Any, get: Callable[[str], Any]) -> list[str]:
    """Return the actions of the workflow runner step, given the tick module and a reader of the pass state."""
    return []


registry.register(
    CallStep(
        "workflow runner",
        "wrapper phases",
        _workflow_runner,
        needs=frozenset(),
        reads=frozenset(),
        writes=frozenset(),
        lane=Lane("slow"),
        effects=frozenset({"host", "inbox", "llm"}),
        feature="swarm-workflow-runner",
    )
)
