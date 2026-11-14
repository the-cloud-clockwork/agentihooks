# ruff: noqa: F811
from collections.abc import Callable

import pytest

from scripts.swarm import cli
from scripts.swarm.ledger_events import PullRequest
from scripts.swarm.store import AgentRecord, RedisStore
from tests.helpers.seat import seat  # noqa: F401
from tests.swarm.test_cli import env, run, started  # noqa: F401
from tests.swarm.test_promotion_watch import GREEN, SLUG, Ledger, gh, refresh, store  # noqa: F401

REOPEN = {"state": "open", "claimed_by": ""}


@pytest.mark.feature("done-clears-awaiting")
def test_swarm_done_clears_awaiting_and_a_reopened_task_shows_no_stale_wait(
    env: tuple, seat: Callable[..., AgentRecord], monkeypatch: pytest.MonkeyPatch
) -> None:
    _, ledger, _ = env
    run("sw", "create", "--repo", "/repo")
    started(env, seat)
    monkeypatch.setenv("AGENTIHOOKS_AGENT_NAME", "engineer@a1b2c3-0001")
    url = "https://github.com/o/r/pull/9"
    ledger.rows["t1"].update(state="pr", awaiting="approval", pr_url=url)
    assert run("sw", "done", "--pr", url) == 0
    assert (ledger.rows["t1"]["state"], ledger.rows["t1"]["awaiting"]) == ("done", "")
    ledger.update_task("sw", "t1", REOPEN, if_state=("done",))
    assert (ledger.rows["t1"]["state"], ledger.rows["t1"]["awaiting"]) == ("open", "")


@pytest.mark.feature("done-clears-awaiting")
def test_swarm_done_on_a_group_lead_clears_awaiting_on_its_members(
    env: tuple, seat: Callable[..., AgentRecord], monkeypatch: pytest.MonkeyPatch
) -> None:
    _, ledger, _ = env
    ledger.rows["t1"]["group_members"] = ["t3"]
    member = {"lane": "eng", "title": "member", "description": "spec", "claimed_by": "", "merged_into": "t1"}
    ledger.rows["t3"] = {"id": "t3", "state": "pr", "awaiting": "verify", **member}
    run("sw", "create", "--repo", "/repo")
    started(env, seat)
    monkeypatch.setenv("AGENTIHOOKS_AGENT_NAME", "engineer@a1b2c3-0001")
    ledger.rows["t1"]["pr_url"] = "https://github.com/o/r/pull/9"
    assert run("sw", "done", "--finding", "one pull request") == 0
    assert (ledger.rows["t3"]["state"], ledger.rows["t3"]["awaiting"]) == ("done", "")


@pytest.mark.feature("done-clears-awaiting")
def test_the_tick_reopens_a_done_task_without_a_stale_wait(
    env: tuple, seat: Callable[..., AgentRecord], monkeypatch: pytest.MonkeyPatch
) -> None:
    swarm, ledger, rt = env
    run("sw", "create", "--repo", "/repo")
    started(env, seat)
    monkeypatch.setenv("AGENTIHOOKS_AGENT_NAME", "engineer@a1b2c3-0001")
    url = "https://github.com/o/r/pull/9"
    ledger.rows["t1"].update(state="pr", awaiting="fix", pr_url=url)
    assert run("sw", "done", "--pr", url) == 0
    run("sw", "stop")
    monkeypatch.setattr(cli, "now_ms", lambda: 9_000_000)
    ledger.log = [{"kind": "task done", "target": "tasks/t1", "by": "engineer@a1b2c3-0001", "at": 8_000_000}]
    ledger.pulls[url] = PullRequest("CLOSED", None, 1, False)
    actions = cli.run_tick(swarm, "sw", ledger, rt, cli.delivery.HerdrMessenger())
    assert "task t1 reopened, its pull request is closed" in actions
    assert (ledger.rows["t1"]["state"], ledger.rows["t1"]["awaiting"]) == ("open", "")


@pytest.mark.feature("done-clears-awaiting")
def test_a_green_promotion_run_clears_awaiting_and_a_reopened_task_shows_no_stale_wait(store: RedisStore) -> None:
    tasks = [{"id": "promo-900-0", "title": "Promotion red: helm-kind", "state": "blocked", "awaiting": "fix"}]
    ledger = Ledger(tasks)
    refresh(store, ledger, gh([GREEN]))
    assert ledger.updates[0][2] == {"state": "done", "awaiting": ""}
    assert (ledger.tasks[0]["state"], ledger.tasks[0]["awaiting"]) == ("done", "")
    ledger.update_task(SLUG, "promo-900-0", REOPEN, if_state=("done",))
    assert (ledger.tasks[0]["state"], ledger.tasks[0]["awaiting"]) == ("open", "")
