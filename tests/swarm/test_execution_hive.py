import json

import pytest

from scripts.swarm.execution import ExecutionRegistry
from scripts.swarm.store import RedisStore, SwarmConfig

pytestmark = pytest.mark.unit


@pytest.fixture
def store():
    import fakeredis

    store = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    store.create(SwarmConfig("fixture", "agentihooks", 1, 0))
    return store


def test_hive_reads_the_latest_placement_of_a_current_agent(store):
    key = store.key("fixture", "executions")
    store.redis.hset(store.key("fixture", "agents"), "eng-a", "{}")
    for generation, hive, name in [(2, "m2", "eng-a"), (1, "m1", "eng-a"), (3, "m3", "eng-b")]:
        store.redis.hset(key, f"exe-{generation}", json.dumps({"name": name, "generation": generation, "hive": hive}))
    registry = ExecutionRegistry(store)

    assert registry.hive("fixture", "eng-a") == "m2"
    assert registry.hive("fixture", "eng-b") == ""
    assert registry.hive("fixture", "nobody") == ""


@pytest.mark.parametrize("rows", [[], [{}], [{"hive": 7}]])
def test_hive_is_empty_for_a_listed_agent_without_a_named_placement(store, rows):
    store.redis.hset(store.key("fixture", "agents"), "eng-a", "{}")
    for generation, row in enumerate(rows, 1):
        store.redis.hset(
            store.key("fixture", "executions"),
            f"exe-{generation}",
            json.dumps({"name": "eng-a", "generation": generation, **row}),
        )

    assert ExecutionRegistry(store).hive("fixture", "eng-a") == ""


def test_an_imported_execution_without_launch_details_reads_as_empty(store):
    store.redis.hset(store.key("fixture", "agents"), "eng-a", json.dumps({"name": "eng-a", "lane": "eng", "task": "t"}))
    store.redis.hset(
        store.key("fixture", "executions"),
        "exe-1",
        json.dumps(
            {
                "name": "eng-a",
                "generation": 1,
                "execution_id": "exe-1",
                "runtime_backend": "local",
                "runtime_target": {},
            }
        ),
    )

    [agent] = ExecutionRegistry(store).agents("fixture")

    assert (agent.hive, agent.transport, agent.remote_control) == ("", "", {})


def test_latest_keeps_the_highest_generation_and_lets_a_later_seat_row_break_a_tie():
    from scripts.swarm.execution import latest

    rows = [
        {"seat": "s", "name": "a", "generation": 2, "row": 1},
        {"seat": "s", "name": "a", "generation": 1, "row": 2},
        {"seat": "s", "name": "a", "generation": 2, "row": 3},
    ]

    assert latest(rows, "seat") == {"s": rows[2]}
    assert latest(rows, "name") == {"a": rows[0]}


def test_latest_never_lets_an_older_generation_replace_a_seat_head():
    from scripts.swarm.execution import latest

    rows = [
        {"seat": "s", "name": "a", "generation": 2, "row": 1},
        {"seat": "s", "name": "a", "generation": 1, "row": 2},
    ]

    assert latest(rows, "seat") == {"s": rows[0]}
    assert latest(rows, "name") == {"a": rows[0]}
