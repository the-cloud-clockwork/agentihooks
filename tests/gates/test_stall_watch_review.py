import json
from dataclasses import replace

import pytest

from scripts.swarm import stall_watch


@pytest.mark.feature("reviewer-worker-gates")
@pytest.mark.parametrize("lane", ["eng", "ci", "review"])
def test_full_worker_lanes_report_unclaimed_work(lane: str) -> None:
    from tests.swarm.test_stall_watch import MINUTE, OWNER, RUNNING, SLUG, START, Swarm

    swarm = Swarm(RUNNING)
    task = swarm.ledger.rows["t1"]
    task.update(state="open", claimed_by="", pr_url="", lane=lane)
    swarm.ledger.rows["t2"] = {"id": "t2", "state": "claimed", "claimed_by": OWNER, "lane": lane}
    swarm.ready = {"eng": [], "ci": [], "review": [], lane: [task]}
    agent = swarm.store.agents(SLUG)[0]
    swarm.store.put_agent(SLUG, replace(agent, lane=lane, task="t2"))
    capacity = {"configured": {"eng": 1, "ci": 1, "plan": 0, "review": 1}, "placeable": {"claude": 1, "codex": 0}}
    swarm.store.redis.set(swarm.store.key(SLUG, "quota-capacity"), json.dumps(capacity))
    swarm.tick(START)
    assert swarm.inbox() == []
    swarm.tick(START + 15 * MINUTE)
    [item] = swarm.inbox()
    assert "unclaimed while its lane is full for 15 minutes" in item.text
    assert item.ref == f"sw:{stall_watch.KIND}:t1:unclaimed:{START}:alert"
    assert swarm.retired == []
    assert task["state"] == "open"


@pytest.mark.feature("reviewer-worker-gates")
def test_a_stale_review_claim_with_a_pull_request_is_reported() -> None:
    from tests.swarm.test_stall_watch import GREEN, MINUTE, SLUG, START, Swarm

    swarm = Swarm(GREEN, task={"lane": "review"}, environ={"AGENTIHOOKS_GATE_STALL": "observe"})
    agent = swarm.store.agents(SLUG)[0]
    swarm.store.put_agent(SLUG, replace(agent, lane="review"))
    swarm.ready["review"] = []
    swarm.tick(START)
    swarm.tick(START + 120 * MINUTE)
    assert any(item.ref.startswith(f"sw:{stall_watch.KIND}:t1:") for item in swarm.inbox())
