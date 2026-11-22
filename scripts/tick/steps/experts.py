"""The experts steps of the tick."""

from collections.abc import Callable
from types import SimpleNamespace
from typing import Any

from scripts.swarm import expert_seat
from scripts.tick import registry
from scripts.tick.step import Lane
from scripts.tick.steps import CallStep, pass_domains
from scripts.tick.steps.host import held

SEAT: Any = SimpleNamespace(expert_seat=expert_seat)


def _expert_seats(cli: Any, get: Callable[[str], Any]) -> list[str]:
    """Return the actions of the expert seats step, given the swarm command module and a reader of the pass state:
    the pass over the expert seats of the swarm, and nothing while the swarm is in panic. A sleeping swarm still
    keeps its expert seats: a lineage waits for no ledger work."""
    if cli.panic.state(get("store"), get("slug")) is not None:
        return []
    actions: list[str] = cli.skip_refused(
        SEAT.expert_seat.run, get("slug"), get("config"), get("store"), get("runtime"), get("now_ms")
    )
    return actions


registry.register(
    CallStep(
        "expert seats",
        "dispatch seat",
        held(_expert_seats),
        needs=frozenset({"drained resume", "reap"}),
        reads=pass_domains("config"),
        writes=frozenset(),
        lane=Lane("tick"),
        effects=frozenset({"coordination", "inbox", "runtime"}),
        feature="expert-seat-step",
        placing=True,
    )
)
