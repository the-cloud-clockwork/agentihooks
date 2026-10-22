import pytest

from scripts.swarm.tick import LEASE_MS, tick
from tests.swarm.test_tick import HiveAcks, store, tasks  # noqa: F401


@pytest.mark.parametrize("broken", [False, True])
def test_a_launch_the_hive_acknowledged_keeps_its_claim_when_collect_fails_at_the_claim_lease(store, broken):  # noqa: F811
    ledger, runtime = tasks(("t1", "eng")), HiveAcks()
    tick("sw", store, ledger, runtime, now_ms=1_000)
    first = runtime.ordered[0]
    runtime.kept.add(first)
    if broken:
        runtime.broken.add(first)
    tick("sw", store, ledger, runtime, now_ms=1_000 + LEASE_MS)
    assert runtime.expired == [first]
    assert [row["state"] for row in store.launches("sw")] == ["acknowledging"]
    assert ledger.rows["t1"]["claimed_by"] == first and store.claimant("sw", "t1") == first
    runtime.broken.discard(first)
    runtime.acked.add(first)
    tick("sw", store, ledger, runtime, now_ms=2_000 + LEASE_MS)
    assert [row["state"] for row in store.launches("sw")] == ["started"]
