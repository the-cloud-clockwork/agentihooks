from dataclasses import replace

import pytest

from scripts.swarm.tick import STARTUP_GRACE_MS, tick
from tests.swarm.test_claim_cap import CommentingLedger
from tests.swarm.test_cli import env, run  # noqa: F401
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


@pytest.mark.parametrize("cause", ["quota", "parked"])
@pytest.mark.parametrize("path", ["parked", "remote"])
def test_a_second_tick_refund_for_the_same_claim_is_refused(store, path, cause):  # noqa: F811
    from scripts.swarm import tick as tick_module

    ledger, runtime = CommentingLedger([{"id": "t1"}, {"id": "t9", "state": "claimed"}]), FakeRuntime()
    tick("sw", store, ledger, runtime, now_ms=1_000)
    agent = replace(next(a for a in workers(store) if a.task == "t1"), state="finished")
    if cause == "quota":
        store.put_handoff("sw", "t1", "continue here", seat=agent.seat, envelope={"reason": "quota"})
    else:
        ledger.rows["t1"]["parked_on"] = ["t9"]
    generation, lives = store.claim_generation("sw", "t1"), store.claims("sw", "t1")
    for _ in range(2):
        store.put_agent("sw", agent)
        if path == "parked":
            tick_module._retire_ended("sw", store, ledger, runtime, ledger.rows, agent, 2_000)
        else:
            tick_module._drop_remote("sw", store, ledger, ledger.rows, agent, "its hive pane is gone")
    assert store.claims("sw", "t1") == lives - 1
    assert store.refund_held_claim("sw", "t1", agent.name, generation) is False


def test_a_second_stop_refund_for_the_same_claim_is_refused(store):  # noqa: F811
    from scripts.swarm.cli import stop_now
    from scripts.swarm.ledger_client import LedgerRefused

    ledger, runtime = CommentingLedger([{"id": "t1"}]), FakeRuntime()
    tick("sw", store, ledger, runtime, now_ms=1_000)
    agent = workers(store)[0]
    store.count_claim("sw", "t1")
    generation, lives = store.claim_generation("sw", "t1"), store.claims("sw", "t1")

    def refused(*args, **kwargs):
        raise LedgerRefused("busy")

    ledger.update_task = refused
    for _ in range(2):
        store.put_agent("sw", agent)
        stop_now(store, "sw", runtime, ledger)
    assert ledger.rows["t1"]["claimed_by"] == agent.name
    assert store.claims("sw", "t1") == lives - 1
    assert store.refund_held_claim("sw", "t1", agent.name, generation) is False


@pytest.mark.parametrize(("state", "finished", "refunded"), [("pr", False, 1), ("claimed", True, 0)])
def test_stop_refunds_a_pull_request_claim_and_never_a_finished_agent(store, state, finished, refunded):  # noqa: F811
    from scripts.swarm.cli import stop_now

    ledger, runtime = CommentingLedger([{"id": "t1"}]), FakeRuntime()
    tick("sw", store, ledger, runtime, now_ms=1_000)
    agent = workers(store)[0]
    store.count_claim("sw", "t1")
    lives = store.claims("sw", "t1")
    ledger.rows["t1"]["state"] = state
    if finished:
        store.put_agent("sw", replace(agent, state="finished"))
    stop_now(store, "sw", runtime, ledger)
    assert store.claims("sw", "t1") == lives - refunded
    assert ledger.rows["t1"]["state"] == ("open" if refunded else state)


def test_a_dev_red_block_refunds_the_held_claim_once(env, monkeypatch):  # noqa: F811
    from types import SimpleNamespace

    from scripts.swarm import cli, dev_red

    swarm, ledger, _ = env
    run("sw", "create", "--repo", "/repo")
    run("sw", "start")
    monkeypatch.setattr(dev_red, "latest", lambda repo, run=None: {"id": 41, "conclusion": "failure"})
    agent = next(a for a in swarm.agents("sw") if a.name == "ci@a1b2c3-0001")
    generation, lives = swarm.claim_generation("sw", "t2"), swarm.claims("sw", "t2")
    assert run("sw", "--as", "ci@a1b2c3-0001", "block", "--dev-red", "dev is red") == 0
    assert swarm.claims("sw", "t2") == lives - 1
    cli.block_agent(swarm, "sw", agent, "dev is red", ledger, 41)
    assert swarm.claims("sw", "t2") == lives - 1
    monkeypatch.setattr(dev_red, "latest", lambda repo, run=None: {"id": 42, "conclusion": "success"})
    rows = {t["id"]: t for t in ledger.tasks("sw")}
    assert dev_red.reopen_pass("sw", SimpleNamespace(repo="/repo"), swarm, ledger, rows) != []
    assert swarm.claims("sw", "t2") == lives - 1
    assert swarm.refund_held_claim("sw", "t2", "ci@a1b2c3-0001", generation) is False
