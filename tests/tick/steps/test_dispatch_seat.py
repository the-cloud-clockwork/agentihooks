"""The dispatch seat tick step holds while fleet panic is on and runs the dispatcher seat once panic clears."""

from contextlib import nullcontext
from types import SimpleNamespace
from typing import Any

import pytest

from scripts.swarm import dispatch_seat
from scripts.tick.steps import masters

pytestmark = pytest.mark.unit


@pytest.mark.feature("swarm-tick-panic-holds-spawn")
def test_dispatch_seat() -> None:
    """Dispatch seat runs no dispatcher seat call while panic holds and runs it once panic clears."""
    calls: list[tuple[Any, ...]] = []
    panic: list[object] = ["halted"]
    fake = SimpleNamespace(
        PLACING=nullcontext(),
        panic=SimpleNamespace(state=lambda store: panic[0]),
        skip_refused=lambda function, *args: calls.append((function, *args)) or ["dispatched"],
    )
    state: dict[str, Any] = {
        "slug": "sw",
        "config": SimpleNamespace(state="running"),
        "store": "store",
        "runtime": "runtime",
        "doc": "doc",
        "now_ms": 7,
        "sleeping": False,
        "box": {},
    }
    step = next(step for step in masters.STEPS if step.name == "dispatch seat")
    assert step.call(fake, state.get) == []
    assert calls == []
    assert masters.RAISED not in state["box"]
    panic[0] = None
    assert step.call(fake, state.get) == ["dispatched"]
    assert calls == [(dispatch_seat.run, "sw", state["config"], "store", "runtime", "doc", 7, False)]
