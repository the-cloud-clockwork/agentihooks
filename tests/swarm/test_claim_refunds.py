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
@pytest.mark.parametrize("prior_life", ["none", "quota", "retirement"])
def test_three_failed_lives_still_block_the_fourth_claim(store, ending, prior_life):  # noqa: F811
    ledger, runtime = CommentingLedger([{"id": "t1"}]), FakeRuntime()
    tick("sw", store, ledger, runtime, now_ms=1_000)
    if prior_life == "quota":
        agent = workers(store)[0]
        store.put_handoff("sw", "t1", "continue here", seat=agent.seat, envelope={"reason": "quota"})
        store.put_agent("sw", replace(agent, state="finished"))
        tick("sw", store, ledger, runtime, now_ms=2_000)
    elif prior_life == "retirement":
        from scripts.swarm.cli import stop_now

        stop_now(store, "sw", runtime, ledger)
        store.update("sw", state="running")
        tick("sw", store, ledger, runtime, now_ms=2_000)
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
    assert [task for _, _, task in runtime.spawned] == ["t1"] * (3 if prior_life == "none" else 4)
    assert ledger.rows["t1"]["state"] == "blocked"
    assert store.claimant("sw", "t1") is None


@pytest.mark.parametrize("ending", ["mismatch", "idle", "stop"])
def test_three_swarm_retirements_allow_a_fourth_claim(store, ending):  # noqa: F811
    from scripts.swarm.cli import stop_now
    from scripts.swarm.tick import IDLE_KILL_TICKS

    ledger, runtime = CommentingLedger([{"id": "t1"}]), FakeRuntime()
    tick("sw", store, ledger, runtime, now_ms=1_000)
    for at in (2_000, 3_000, 4_000):
        agent = workers(store)[0]
        tick("sw", store, ledger, runtime, now_ms=at)
        if ending == "stop":
            stop_now(store, "sw", runtime, ledger)
            store.update("sw", state="running")
        elif ending == "mismatch":
            store.put_agent("sw", replace(agent, account="old-account"))
            original = runtime.bindings
            runtime.bindings = lambda agents: {
                name: {**facts, "account": "new-account"} for name, facts in original(agents).items()
            }
        else:
            runtime.statuses[agent.name] = "idle"
            store.put_agent("sw", replace(agent, idle_ticks=IDLE_KILL_TICKS - 1))
        tick("sw", store, ledger, runtime, now_ms=at + 1)
        if ending == "mismatch":
            runtime.bindings = original
    assert [task for _, _, task in runtime.spawned] == ["t1"] * 4
    assert ledger.rows["t1"]["state"] == "claimed"
    assert store.claims("sw", "t1") == 1


@pytest.mark.parametrize("ending", ["idle", "stop"])
def test_retirement_does_not_refund_an_already_reset_claim(store, ending):  # noqa: F811
    from scripts.swarm.cli import stop_now
    from scripts.swarm.tick import IDLE_KILL_TICKS

    ledger, runtime = CommentingLedger([{"id": "t1"}]), FakeRuntime()
    tick("sw", store, ledger, runtime, now_ms=1_000)
    tick("sw", store, ledger, runtime, now_ms=2_000)
    agent = workers(store)[0]
    store.reset_claims("sw", "t1")
    store.update("sw", state="paused")
    if ending == "stop":
        stop_now(store, "sw", runtime, ledger)
    else:
        runtime.statuses[agent.name] = "idle"
        store.put_agent("sw", replace(agent, idle_ticks=IDLE_KILL_TICKS - 1))
        tick("sw", store, ledger, runtime, now_ms=3_000)
    assert store.claims("sw", "t1") == 0
    assert agent.name in runtime.killed


def test_stop_refunds_nothing_once_the_agent_no_longer_holds_its_claim(store):  # noqa: F811
    from scripts.swarm.cli import stop_now

    ledger, runtime = CommentingLedger([{"id": "t1"}]), FakeRuntime()
    tick("sw", store, ledger, runtime, now_ms=1_000)
    agent = workers(store)[0]
    lives = store.claims("sw", "t1")
    assert store.release("sw", "t1", agent.name)
    stop_now(store, "sw", runtime, ledger)
    assert store.claims("sw", "t1") == lives
    assert agent.name in runtime.killed


@pytest.mark.parametrize(
    "agent_state, row_state, refunded",
    [("working", "claimed", True), ("working", "pr", True), ("finished", "claimed", False), ("working", "done", False)],
)
def test_stop_refunds_and_reopens_only_a_held_claimed_or_pr_task(store, agent_state, row_state, refunded):  # noqa: F811
    from scripts.swarm.cli import stop_now

    ledger, runtime = CommentingLedger([{"id": "t1"}]), FakeRuntime()
    tick("sw", store, ledger, runtime, now_ms=1_000)
    agent = workers(store)[0]
    store.put_agent("sw", replace(agent, state=agent_state))
    ledger.rows["t1"]["state"] = row_state
    lives = store.claims("sw", "t1")
    stop_now(store, "sw", runtime, ledger)
    assert store.claims("sw", "t1") == lives - refunded
    assert ledger.rows["t1"]["state"] == ("open" if refunded else row_state)


def test_a_parked_retirement_refunds_nothing_once_the_agent_no_longer_holds_its_claim(store):  # noqa: F811
    ledger, runtime = CommentingLedger([{"id": "t1"}]), FakeRuntime()
    tick("sw", store, ledger, runtime, now_ms=1_000)
    agent = workers(store)[0]
    store.put_handoff("sw", "t1", "continue here", seat=agent.seat, envelope={"reason": "quota"})
    store.put_agent("sw", replace(agent, state="finished"))
    assert store.release("sw", "t1", agent.name)
    lives, spawned = store.claims("sw", "t1"), len(runtime.spawned)
    tick("sw", store, ledger, runtime, now_ms=2_000)
    assert agent.name in runtime.killed
    new_claims = len(runtime.spawned) - spawned
    refunded = lives + new_claims - store.claims("sw", "t1")
    assert refunded == 0
