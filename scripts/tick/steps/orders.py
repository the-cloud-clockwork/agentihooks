"""The orders steps of the tick: note each operator order nobody works on."""

from typing import Any

from scripts.swarm import timing
from scripts.tick import registry
from scripts.tick.step import Lane
from scripts.tick.steps import CallStep, hook_domains
from scripts.tick.steps.host import Get, held


def _order_waiting(cli: Any, get: Get) -> list[str]:
    """Note each open operator order whose unfinished tasks have nothing claimed or in a pull request."""
    from scripts.swarm.health import order_waiting

    timing.call(order_waiting.observe, get("store"), get("slug"), get("doc"), cli.now_ms())  # pyright: ignore[reportUnknownMemberType]
    return []


registry.register(
    CallStep(
        "order waiting",
        "wrapper phases",
        held(_order_waiting),
        needs=frozenset(),
        reads=hook_domains("doc"),
        writes=frozenset(),
        lane=Lane("slow"),
        effects=frozenset({"coordination"}),
        feature="swarm-order-waiting-finding",
    )
)
