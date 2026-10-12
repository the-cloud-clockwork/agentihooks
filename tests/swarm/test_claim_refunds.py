from dataclasses import replace

import pytest

from scripts.swarm.tick import STARTUP_GRACE_MS, tick
from tests.swarm.test_claim_cap import CommentingLedger
from tests.swarm.test_tick import FakeRuntime, store, workers  # noqa: F401

pytestmark = pytest.mark.unit


def test_three_quota_handoffs_allow_a_fourth_claim(store):  # noqa: F811
    ledger, runtime = CommentingLedger([{"id": "t1"}]), FakeRuntime()
    tick("sw", store, ledger, runtime, now_ms=1_000)
    for at in (2_000, 3_000, 4_000):
        agent = workers(store)[0]
        store.put_handoff("sw", "t1", "continue here", seat=agent.seat, envelope={"reason": "quota"})
        store.put_agent("sw", replace(agent, state="finished"))
        tick("sw", store, ledger, runtime, now_ms=at)
    assert [task for _, _, task in runtime.spawned] == ["t1"] * 4
    assert ledger.rows["t1"]["state"] == "claimed"
    assert store.claims("sw", "t1") == 1


@pytest.mark.parametrize("ending", ["block", "exit"])
def test_three_failed_lives_still_block_the_fourth_claim(store, ending):  # noqa: F811
    ledger, runtime = CommentingLedger([{"id": "t1"}]), FakeRuntime()
    tick("sw", store, ledger, runtime, now_ms=1_000)
    for at in (100_000, 200_000, 300_000):
        agent = workers(store)[0]
        if ending == "block":
            ledger.rows["t1"]["state"] = "blocked"
            store.put_agent("sw", replace(agent, state="finished"))
            tick("sw", store, ledger, runtime, now_ms=at)
            ledger.rows["t1"].update(state="open", claimed_by="")
            tick("sw", store, ledger, runtime, now_ms=at + 1)
        else:
            tick("sw", store, ledger, runtime, now_ms=agent.started_at + 1)
            runtime.live.discard(agent.name)
            tick("sw", store, ledger, runtime, now_ms=agent.started_at + STARTUP_GRACE_MS + 1)
    assert [task for _, _, task in runtime.spawned] == ["t1"] * 3
    assert ledger.rows["t1"]["state"] == "blocked"
    assert store.claimant("sw", "t1") is None
