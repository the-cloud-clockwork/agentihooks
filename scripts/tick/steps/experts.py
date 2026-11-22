"""The experts steps of the tick."""

from collections.abc import Callable
from types import SimpleNamespace
from typing import Any

from scripts.swarm import expert_seat
from scripts.swarm.ledger_client import LedgerOffline
from scripts.tick import registry
from scripts.tick.step import Lane
from scripts.tick.steps import CallStep, Left, hold, pass_domains

SEAT: Any = SimpleNamespace(expert_seat=expert_seat)


def _expert_seats(tick: Any, get: Callable[[str], Any]) -> list[str]:
    """Return the actions of the expert seats step, given the tick module and a reader of the pass state: the pass
    over the expert seats of the swarm, and nothing while the swarm is in panic."""
    if tick.panic.state(get("store"), get("slug")) is not None:
        return []
    actions: list[str] = tick.skip_refused(
        SEAT.expert_seat.run, get("slug"), get("config"), get("store"), get("runtime"), get("now_ms")
    )
    return actions


def _held(call: Callable[[Any, Callable[[str], Any]], list[str]]) -> Callable[[Any, Callable[[str], Any]], list[str]]:
    """Wrap the step's call to hold the exception it raises, so the pass goes on and ends on it."""

    def run(tick: Any, get: Callable[[str], Any]) -> list[str]:
        box = get("box")
        try:
            return call(tick, get)
        except (Left, LedgerOffline):
            return []
        except (Exception, SystemExit) as exc:
            hold(box, exc)
            return []

    return run


registry.register(
    CallStep(
        "expert seats",
        "dispatch seat",
        _held(_expert_seats),
        needs=frozenset({"drained resume"}),
        reads=pass_domains("config"),
        writes=frozenset(),
        lane=Lane("tick"),
        effects=frozenset({"coordination", "inbox", "runtime"}),
        feature="expert-seat-step",
        placing=True,
    )
)
