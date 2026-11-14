from dataclasses import replace
from typing import Any, NoReturn

import pytest

from scripts.swarm.store import AgentRecord, RedisStore
from scripts.swarm.tick import tick
from tests.helpers import store_seed
from tests.helpers.seat import seat_agent
from tests.swarm.test_cli import env, run  # noqa: F401
from tests.swarm.test_tick import FakeLedger, FakeRuntime, store, workers  # noqa: F401

pytestmark = pytest.mark.unit


class CommentingLedger(FakeLedger):
    """A fake ledger whose state lists its tasks and that keeps every comment written to it."""

    def __init__(self, tasks: list[dict[str, Any]]) -> None:
        super().__init__(tasks)
        self.comments: list[tuple[str, str, str, str]] = []

    def state(self, slug: str) -> dict[str, Any]:
        return {**super().state(slug), "tasks": [dict(row) for row in self.rows.values()]}

    def update_task(
        self,
        slug: str,
        task_id: str,
        fields: dict[str, Any],
        by: str = "swarm",
        if_state: tuple[str, ...] = (),
        if_match: Any = None,
    ) -> Any:
        assert (slug, by) == ("sw", "swarm")
        return super().update_task(slug, task_id, fields, by, if_state, if_match)

    def comment(self, slug: str, task_id: str, text: str, by: str) -> None:
        self.comments.append((slug, task_id, text, by))


def launched(store: RedisStore, runtime: FakeRuntime, agent: AgentRecord) -> AgentRecord:  # noqa: F811
    """The seated ``agent`` as a started launch leaves it: its claim life counted, its record working on a pane from
    its start time and its name live in ``runtime``."""
    store_seed.count_claim(store, "sw", agent.task)
    working = replace(agent, state="working", pane_id="w1:p1", launched_at=agent.started_at)
    store.put_agent("sw", working)
    runtime.live.add(agent.name)
    return working


@pytest.mark.parametrize("ending", ["idle", "stop"])
def test_retirement_does_not_refund_an_already_reset_claim(store: RedisStore, ending: str) -> None:  # noqa: F811
    from scripts.swarm.cli import stop_now
    from scripts.swarm.tick import IDLE_KILL_TICKS

    ledger, runtime = CommentingLedger([{"id": "t1"}]), FakeRuntime()
    agent = launched(store, runtime, seat_agent(store, ledger, "sw", "t1"))
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


def test_stop_refunds_nothing_once_the_agent_no_longer_holds_its_claim(store: RedisStore) -> None:  # noqa: F811
    from scripts.swarm.cli import stop_now

    ledger, runtime = CommentingLedger([{"id": "t1"}]), FakeRuntime()
    agent = launched(store, runtime, seat_agent(store, ledger, "sw", "t1"))
    lives = store.claims("sw", "t1")
    assert store.release("sw", "t1", agent.name)
    stop_now(store, "sw", runtime, ledger)
    assert store.claims("sw", "t1") == lives
    assert agent.name in runtime.killed


@pytest.mark.parametrize(
    "agent_state, row_state, refunded",
    [("working", "claimed", True), ("working", "pr", True), ("finished", "claimed", False), ("working", "done", False)],
)
def test_stop_refunds_and_reopens_only_a_held_claimed_or_pr_task(
    store: RedisStore,  # noqa: F811
    agent_state: str,
    row_state: str,
    refunded: bool,
) -> None:
    from scripts.swarm.cli import stop_now

    ledger, runtime = CommentingLedger([{"id": "t1"}]), FakeRuntime()
    agent = launched(store, runtime, seat_agent(store, ledger, "sw", "t1"))
    store.put_agent("sw", replace(agent, state=agent_state))
    ledger.rows["t1"]["state"] = row_state
    lives = store.claims("sw", "t1")
    stop_now(store, "sw", runtime, ledger)
    assert store.claims("sw", "t1") == lives - refunded
    assert ledger.rows["t1"]["state"] == ("open" if refunded else row_state)


@pytest.mark.parametrize("cause", ["quota", "parked"])
@pytest.mark.parametrize("path", ["parked", "remote"])
def test_a_second_tick_refund_for_the_same_claim_is_refused(store: RedisStore, path: str, cause: str) -> None:  # noqa: F811
    from scripts.swarm import tick as tick_module

    ledger, runtime = CommentingLedger([{"id": "t1"}, {"id": "t9", "state": "claimed"}]), FakeRuntime()
    agent = replace(launched(store, runtime, seat_agent(store, ledger, "sw", "t1")), state="finished")
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


def test_a_second_stop_refund_for_the_same_claim_is_refused(store: RedisStore) -> None:  # noqa: F811
    from scripts.swarm.cli import stop_now
    from scripts.swarm.ledger_client import LedgerRefused

    ledger, runtime = CommentingLedger([{"id": "t1"}]), FakeRuntime()
    agent = launched(store, runtime, seat_agent(store, ledger, "sw", "t1"))
    store_seed.count_claim(store, "sw", "t1")
    generation, lives = store.claim_generation("sw", "t1"), store.claims("sw", "t1")

    def refused(*args: Any, **kwargs: Any) -> NoReturn:
        """A ledger write the ledger refuses."""
        raise LedgerRefused("busy")

    ledger.update_task = refused
    for _ in range(2):
        store.put_agent("sw", agent)
        stop_now(store, "sw", runtime, ledger)
    assert ledger.rows["t1"]["claimed_by"] == agent.name
    assert store.claims("sw", "t1") == lives - 1
    assert store.refund_held_claim("sw", "t1", agent.name, generation) is False


def test_a_dev_red_block_refunds_the_held_claim_once(
    env: tuple[RedisStore, Any, FakeRuntime],  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from types import SimpleNamespace

    from scripts.swarm import cli, dev_red

    swarm, ledger, rt = env
    run("sw", "create", "--repo", "/repo")
    agent = launched(swarm, rt, seat_agent(swarm, ledger, "sw", "t2"))
    assert agent.name == "ci@a1b2c3-0001"
    monkeypatch.setattr(dev_red, "latest", lambda repo, run=None: {"id": 41, "conclusion": "failure"})
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


def test_a_dev_red_block_whose_ledger_write_fails_refunds_nothing(
    env: tuple[RedisStore, Any, FakeRuntime],  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from scripts.swarm import cli
    from scripts.swarm.ledger_client import LedgerRefused

    swarm, ledger, rt = env
    run("sw", "create", "--repo", "/repo")
    agent = launched(swarm, rt, seat_agent(swarm, ledger, "sw", "t2"))
    lives = swarm.claims("sw", "t2")

    def refused(*args: Any, **kwargs: Any) -> NoReturn:
        """A ledger write the ledger refuses."""
        raise LedgerRefused("busy")

    monkeypatch.setattr(ledger, "update_task", refused)
    with pytest.raises(LedgerRefused):
        cli.block_agent(swarm, "sw", agent, "dev is red", ledger, 41)
    assert swarm.claims("sw", "t2") == lives
    assert swarm.claimant("sw", "t2") == agent.name
