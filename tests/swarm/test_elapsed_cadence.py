import pytest

from scripts.swarm import ledger_probe
from scripts.swarm.health.findings import idle_with_claim, limits
from scripts.swarm.pane import PaneObservation
from scripts.swarm.store import AgentRecord
from scripts.swarm.tick import _watch_idle
from tests.swarm.test_ledger_probe import FACTS, Clock, ProbedLedger
from tests.swarm.test_ledger_probe import store as store
from tests.swarm.test_tick import FakeLedger, FakeRuntime

MINUTE = 60_000
NAME = "engineer@a1b2c3-0002"


def first_pass_at_or_after(period_ms: int, wall_ms: int) -> int:
    return -(-wall_ms // period_ms) * period_ms


@pytest.mark.parametrize("period_ms", [MINUTE, 3 * MINUTE])
def test_the_ledger_slow_alert_raises_at_the_first_pass_a_minute_after_the_first_slow_pass(store, period_ms):
    clock = Clock()
    ledger = ProbedLedger(clock, read_s=6.0)
    raised = [
        n * period_ms
        for n in range(4)
        if ledger_probe.observe(store, "sw", ledger, FakeRuntime(), n * period_ms, clock=clock, facts=lambda: FACTS)
    ]
    assert raised == [first_pass_at_or_after(period_ms, MINUTE)]


@pytest.mark.parametrize("period_ms", [MINUTE, 3 * MINUTE])
def test_the_idle_with_claim_finding_lands_at_the_same_wall_time_at_either_pass(store, period_ms):
    store.put_agent("sw", AgentRecord(NAME, "eng", "t1"))
    runtime = FakeRuntime()
    runtime.observe = lambda agent: PaneObservation("idle")
    held = {"t1": {"id": "t1", "state": "claimed"}}
    found_at = None
    for n in range(6):
        at = n * period_ms
        _watch_idle("sw", store, FakeLedger([]), runtime, {}, next(a for a in store.agents("sw") if a.name == NAME), at)
        row = next(a for a in store.agents("sw") if a.name == NAME).__dict__
        if found_at is None and idle_with_claim([row], held, limits({})):
            found_at = at
    assert found_at == first_pass_at_or_after(period_ms, 2 * MINUTE)
