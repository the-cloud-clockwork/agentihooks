import json
from typing import Any

import pytest

from scripts.swarm import fencing, tick
from scripts.swarm.store import AgentRecord, RedisStore, SwarmConfig
from tests.swarm.test_tick import FakeLedger


@pytest.mark.feature("swarm-tick-dead-remote-budget")
def test_retiring_an_archived_projection_preserves_its_history_metadata() -> None:
    import fakeredis

    store = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    store.create(SwarmConfig("sw", "/repo", max_eng=1, max_ci=1))
    agent = store.start_execution(
        "sw", AgentRecord(store.next_name("sw", "eng"), "eng", "t1", seat="eng-1@sw", hive="rolled")
    )

    def archive(doc: dict[str, Any]) -> None:
        doc["archive"][agent.execution_id] = doc["executions"].pop(agent.execution_id)

    fencing.write(store, "sw", archive)
    row = json.loads(store.redis.hget(store.key("sw", "agents"), agent.name))
    row = {key: value for key, value in row.items() if key not in ("execution_id", "hive", "runtime_backend")}
    store.redis.hset(store.key("sw", "agents"), agent.name, json.dumps(row))
    assert store.drop_agent("sw", agent.name)
    history = json.loads(store.redis.lindex(store.key("sw", "history"), -1))
    assert (history["execution_id"], history["hive"], history["runtime_backend"]) == (
        agent.execution_id,
        "rolled",
        agent.runtime_backend,
    )


@pytest.mark.feature("swarm-tick-dead-remote-budget")
def test_ten_dead_remote_seats_drop_without_loading_the_execution_archive(monkeypatch: pytest.MonkeyPatch) -> None:
    import fakeredis

    store = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    store.create(SwarmConfig("sw", "/repo", max_eng=1, max_ci=1))
    ledger = FakeLedger([{"id": f"t{index}"} for index in range(10)])
    dead = [
        store.start_execution(
            "sw",
            AgentRecord(store.next_name("sw", "eng"), "eng", f"t{index}", seat=f"eng-{index + 1}@sw", hive="rolled"),
        )
        for index in range(10)
    ]
    for agent in dead:
        ledger.rows[agent.task].update(state="claimed", claimed_by=agent.name)
    read = fencing.read
    archive_reads: list[str] = []

    def archive_read(store: Any, slug: str) -> dict[str, Any]:
        archive_reads.append(slug)
        return read(store, slug)

    monkeypatch.setattr(fencing, "read", archive_read)
    for agent in dead:
        assert "reopened" in tick._drop_remote("sw", store, ledger, ledger.rows, agent, "its hive rolled")
    assert archive_reads == [], "retiring dead seats must not repeatedly hydrate the full execution archive"
    assert store.agents("sw") == []
    assert all(row["state"] == "open" for row in ledger.rows.values())
