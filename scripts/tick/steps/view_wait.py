"""The view wait step of the tick wrapper: the wait for the pass view build, on its own step thread."""

from collections.abc import Callable
from typing import Any

from scripts.tick import registry
from scripts.tick.step import Lane
from scripts.tick.steps import CallStep, host, pass_domains


def _view_wait(cli: Any, get: Callable[[str], Any]) -> None:
    """Store the pass view the intent check started building, waiting for it as ``host.pass_view`` bounds, when the
    intent check stored it."""
    judging = get(host.JUDGING)
    if "judged" in judging:
        view, _ = judging["judged"]
        get("box")["view"] = host.pass_view(get("slug"), view)


registry.register(
    CallStep(
        "view wait",
        "wrapper relay",
        host.held(_view_wait),
        needs=frozenset({"intent check"}),
        reads=pass_domains("judged"),
        writes=pass_domains("view"),
        lane=Lane("tick"),
        effects=frozenset(),
        feature="tick-view-wait-off-scheduler",
        kept=True,
    )
)
