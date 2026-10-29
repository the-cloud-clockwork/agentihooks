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
