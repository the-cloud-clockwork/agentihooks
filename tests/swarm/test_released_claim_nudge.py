from typing import Any

from scripts.swarm.store import RedisStore
from scripts.swarm.tick import tick
from tests.helpers.seat import seat_owner
from tests.swarm.test_tick import IDLER, STALE_MS, FakeRuntime, stale_nudges, store, tasks  # noqa: F401


def seated_idler(store: RedisStore) -> tuple[Any, FakeRuntime]:  # noqa: F811
    """Seats an idle engineer on t1 through hub admission on the tick's hive, launched at 1000, in a paused swarm."""
    ledger, runtime = tasks(("t1", "eng")), FakeRuntime()
    seat_owner(store, ledger, runtime, "t1", 1_000)
    store.update("sw", state="paused")
    runtime.statuses[IDLER] = "idle"
    return ledger, runtime


def test_an_agent_whose_claim_was_released_is_never_nudged_for_the_task(store: RedisStore) -> None:  # noqa: F811
    ledger, runtime = seated_idler(store)
    ledger.rows["t1"].update(claimed_by="engineer@a1b2c3-0009", claim_generation=9)
    tick("sw", store, ledger, runtime, now_ms=1_001 + STALE_MS)
    assert stale_nudges(store) == [] and runtime.nudged == []


def test_an_agent_still_holding_its_claim_is_nudged(store: RedisStore) -> None:  # noqa: F811
    ledger, runtime = seated_idler(store)
    tick("sw", store, ledger, runtime, now_ms=1_001 + STALE_MS)
    assert len(stale_nudges(store)) == 1
