import json
from typing import Any

import pytest

pytestmark = pytest.mark.xdist_group("fakeredis")


@pytest.fixture
def store() -> Any:
    import fakeredis

    from scripts.swarm.models import SwarmConfig
    from scripts.swarm.store import RedisStore

    result = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    result.create(SwarmConfig("fixture", "agentihooks", 1, 0))
    return result


def test_migration_moves_history_outside_fenced_writes(store: Any) -> None:
    from scripts.swarm import fencing

    rows = {str(index): {"execution_id": str(index), "payload": "x" * 2048} for index in range(4017)}
    fencing.write(store, "fixture", lambda doc: doc["archive"].update(rows))
    before = fencing._states(store).get("fixture")
    assert len(before.value) > 8_000_000

    fencing.migrate_archive(store, "fixture")

    entry = fencing._states(store).get("fixture")
    assert len(entry.value) < 1_000_000
    assert json.loads(entry.value)["archive"] == {}
    assert fencing.read(store, "fixture")["archive"] == rows
    fencing.write(store, "fixture", lambda doc: doc["counters"].update({"sample": 1}))
    assert fencing.read(store, "fixture")["archive"] == rows
    assert fencing.read(store, "fixture")["counters"]["sample"] == 1
