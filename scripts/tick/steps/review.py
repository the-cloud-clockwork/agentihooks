"""The review steps of the tick."""

from collections.abc import Callable
from typing import Any

from scripts.tick import registry
from scripts.tick.step import Lane
from scripts.tick.steps import CallStep


def _review_pass(tick: Any, get: Callable[[str], Any]) -> list[str]:
    """Return the actions of the review pass step, given the tick module and a reader of the pass state."""
    return []


registry.register(
    CallStep(
        "review pass",
        "wrapper phases",
        _review_pass,
        needs=frozenset(),
        reads=frozenset(),
        writes=frozenset(),
        lane=Lane("tick"),
        effects=frozenset({"ledger"}),
        feature="swarm-review-trigger",
    )
)
