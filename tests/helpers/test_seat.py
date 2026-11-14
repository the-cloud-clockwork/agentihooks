from typing import Any

import pytest

from scripts.swarm.store import RedisStore
from tests.helpers.seat import LAUNCH, seat
from tests.swarm.test_cli import env, run

_fixtures = (env, seat)


@pytest.mark.feature("test-seat-through-admission")
def test_seat_claims_and_launches_an_agent_without_start_or_a_tick(env: tuple[RedisStore, Any, Any], seat: Any) -> None:
    store, ledger, rt = env
    assert run("sw", "create", "--repo", "/repo") == 0

    agent = seat("t2")

    assert agent.name == "ci@a1b2c3-0001"
    assert (agent.lane, agent.task, agent.seat, agent.state) == ("ci", "t2", "ci-1@sw", "starting")
    assert {field: getattr(agent, field) for field in LAUNCH} == LAUNCH
    assert store.claimant("sw", "t2") == agent.name
    assert (ledger.rows["t2"]["state"], ledger.rows["t2"]["claimed_by"]) == ("claimed", agent.name)
    assert ledger.rows["t1"]["claimed_by"] == ""
    assert store.config("sw").state == "paused"
    assert rt.spawned == []


@pytest.mark.feature("test-seat-through-admission")
def test_seat_carries_a_given_hive_and_launch_decision(env: tuple[RedisStore, Any, Any], seat: Any) -> None:
    store, _, _ = env
    run("sw", "create", "--repo", "/repo")
    launch = {"harness": "codex", "profile": "engineer", "account": "spare", "model": "gpt-5", "effort": "high"}

    agent = seat("t1", hive="hive-a", launch=launch)

    assert (agent.name, agent.hive) == ("engineer@a1b2c3-0001", "hive-a")
    assert {field: getattr(agent, field) for field in launch} == launch
    assert [seated.name for seated in store.agents("sw")] == [agent.name]


def test_seat_fails_the_test_when_admission_refuses(env: tuple[RedisStore, Any, Any], seat: Any) -> None:
    _, ledger, _ = env
    run("sw", "create", "--repo", "/repo")
    ledger.rows["t1"]["state"] = "done"

    with pytest.raises(pytest.fail.Exception, match="^hub admission refused t1: None$"):
        seat("t1")
