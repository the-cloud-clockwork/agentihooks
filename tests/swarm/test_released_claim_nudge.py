from scripts.swarm.tick import tick
from tests.swarm.test_tick import STALE_MS, spawned, stale_nudges, store  # noqa: F401


def test_an_agent_whose_claim_was_released_is_never_nudged_for_the_task(store):  # noqa: F811
    ledger, runtime = spawned(store)
    ledger.rows["t1"].update(claimed_by="engineer@a1b2c3-0009", claim_generation=9)
    tick("sw", store, ledger, runtime, now_ms=1_001 + STALE_MS)
    assert stale_nudges(store) == [] and runtime.nudged == []


def test_an_agent_still_holding_its_claim_is_nudged(store):  # noqa: F811
    ledger, runtime = spawned(store)
    tick("sw", store, ledger, runtime, now_ms=1_001 + STALE_MS)
    assert len(stale_nudges(store)) == 1
