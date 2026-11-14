from typing import Any

import pytest

from scripts.swarm.store import RedisStore
from tests.helpers.seat import seat
from tests.swarm.test_cli import env, run

_fixtures = (env, seat)
FIELDS = ("harness", "profile", "account", "model", "effort")


def launched(agent: Any) -> dict[str, str]:
    """The launch decision fields of a seated agent."""
    return {field: getattr(agent, field) for field in FIELDS}


@pytest.mark.feature("test-seat-through-admission")
def test_seat_claims_and_launches_an_agent_without_start_or_a_tick(env: tuple[RedisStore, Any, Any], seat: Any) -> None:
    store, ledger, rt = env
    assert run("sw", "create", "--repo", "/repo") == 0

    agent = seat("t2")

    assert agent.name == "ci@a1b2c3-0001"
    assert (agent.lane, agent.task, agent.seat, agent.hive) == ("ci", "t2", "ci-1@sw", "root")
    assert (agent.state, agent.pane_id, agent.launched_at) == ("working", "w1:p1", 1_000)
    assert store.redis.hget(store.key("sw", "pending-lives"), "t2") is None
    assert store.claims("sw", "t2") == 1
    assert store.redis.hget(store.key("sw", "launches"), agent.name) is None
    assert launched(agent) == {
        "harness": "claude",
        "profile": "cicd",
        "account": "acct",
        "model": "opus",
        "effort": "high",
    }
    assert store.claimant("sw", "t2") == agent.name
    assert (ledger.rows["t2"]["state"], ledger.rows["t2"]["claimed_by"]) == ("claimed", agent.name)
    assert ledger.rows["t1"]["claimed_by"] == ""
    assert store.config("sw").state == "paused"
    assert (rt.spawned, rt.live) == ([], set())


@pytest.mark.feature("test-seat-through-admission")
def test_seat_carries_a_given_hive_and_overrides_only_the_named_launch_fields(
    env: tuple[RedisStore, Any, Any], seat: Any
) -> None:
    store, _, _ = env
    run("sw", "create", "--repo", "/repo")

    agent = seat("t1", hive="hive-a", launch={"harness": "codex", "model": "gpt-5"})

    assert (agent.name, agent.hive) == ("engineer@a1b2c3-0001", "hive-a")
    assert launched(agent) == {
        "harness": "codex",
        "profile": "engineer",
        "account": "acct",
        "model": "gpt-5",
        "effort": "high",
    }
    assert [seated.name for seated in store.agents("sw")] == [agent.name]


@pytest.mark.feature("test-seat-through-admission")
def test_seat_fails_the_test_when_admission_refuses(env: tuple[RedisStore, Any, Any], seat: Any) -> None:
    _, ledger, _ = env
    run("sw", "create", "--repo", "/repo")
    ledger.rows["t1"]["state"] = "done"

    with pytest.raises(pytest.fail.Exception) as refused:
        seat("t1")

    assert str(refused.value) == "hub admission refused t1: None"
