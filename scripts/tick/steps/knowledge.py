"""The knowledge steps of the tick: keep each running swarm's knowledge index level with its ledger."""

from collections.abc import Callable
from typing import Any

from scripts.tick import registry
from scripts.tick.step import Lane
from scripts.tick.steps import CallStep


def _knowledge_warm(tick: Any, get: Callable[[str], Any]) -> None:
    """Start the detached refresh that embeds the swarm's new and changed ledger items, so an ask finds it fresh."""
    get("tools").host.run({"job": "knowledge"}, deadline=get("deadline"))


registry.register(
    CallStep(
        "knowledge warm",
        "wrapper metrics",
        _knowledge_warm,
        needs=frozenset(),
        reads=frozenset(),
        writes=frozenset(),
        lane=Lane("slow"),
        effects=frozenset({"host"}),
        feature="tick-knowledge-warm",
    )
)
