from copy import copy
from typing import Any

import pytest

from scripts.swarm import ledger_events
from scripts.tick import engine, pool
from scripts.tick.deadline import Deadline
from scripts.tick.sites import _GraphView
from scripts.tick.step import Context
from scripts.tick.steps import host

pytestmark = pytest.mark.unit


@pytest.mark.feature("tick-merged-task-settlement-after-overrun")
def test_tick_view(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[tuple[Any, ...]] = []
    lookup = {"merged": object()}.get

    def read(*args: Any) -> Any:
        seen.append(args)
        return lookup

    monkeypatch.setattr(ledger_events, "tick_view", read)
    state = {"inbox": "inbox", "store": "store", "slug": "sw", "doc": {"tasks": []}, "view": None}
    box: dict[str, Any] = {}
    step = copy(next(step for step in host.STEPS if step.name == "tick view"))
    step.interval_s = 0.0
    with pool.running(critical_s=0.0, slow_s=1.0):
        records = engine.run_graph((step,), Context(_GraphView(state, box), Deadline.after(5.0)))
    assert records[0].outcome == engine.OK
    assert box["view"] is lookup
    assert seen == [("inbox", "store", "sw", {"tasks": []}, None)]
