"""The step finished line logs the budget the engine joined the step on, read in the pass's own context."""

import json
from dataclasses import dataclass
from types import MappingProxyType

import pytest

from scripts.swarm import timing
from scripts.swarm.store import RedisStore
from scripts.tick import engine
from scripts.tick.deadline import Deadline
from scripts.tick.step import Context, Domain, Lane, Result
from scripts.tick.steps import budgets
from tests.tick.test_engine import View

pytestmark = pytest.mark.unit

FLOOR_S = 2.0
LIVE_S = 7.0


@dataclass
class LiveBudgetStep:
    """A step whose budget, like a registered step's, is its live budget this pass, never under its floor."""

    name: str = "live budget"
    group: str = "tasks"
    lane: Lane = Lane("main")
    interval_s: float = 0.0
    needs: frozenset[str] = frozenset()
    reads: frozenset[Domain] = frozenset()
    writes: frozenset[Domain] = frozenset()
    effects: frozenset[str] = frozenset()
    feature: str = ""

    @property
    def budget_s(self) -> float:
        return budgets.floored(self.name, FLOOR_S)

    def run(self, ctx: Context) -> Result:
        return Result()


@pytest.mark.feature("swarm-tick-joined-budget")
def test_the_step_finished_line_logs_the_live_budget_the_engine_joined_on() -> None:
    import fakeredis

    store = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    token = budgets.LIVE.set(MappingProxyType({"live budget": LIVE_S}))
    try:
        with timing.tick("sw", store), timing.pass_record(store, "sw"):
            record = engine.run_step(LiveBudgetStep(), Context(View(), Deadline.after(60.0)))
    finally:
        budgets.LIVE.reset(token)
    rows = [json.loads(fields["record"]) for _, fields in store.redis.xrange(store.key("sw", "tick:steps"))]
    assert record.budget_s == LIVE_S
    assert [(row["step"], row["phase"], row["budget_s"], row["late"]) for row in rows] == [
        ("live budget", "finished", LIVE_S, False)
    ]
