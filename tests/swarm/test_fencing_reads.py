import json
from dataclasses import asdict
from typing import Any

import pytest

from scripts.hive import scope
from scripts.swarm import fencing
from scripts.swarm.store import AgentRecord, RedisStore

pytestmark = pytest.mark.unit


@pytest.mark.parametrize("archive_size", [1, 200])
def test_routing_and_roster_reads_do_not_transfer_the_execution_archive(
    monkeypatch: pytest.MonkeyPatch, archive_size: int
) -> None:
    from fakeredis import FakeRedis

    store = RedisStore(FakeRedis(decode_responses=True))
    agent = AgentRecord("eng-a", "eng", "task", execution_id="active", generation=2, hive="hive-a")
    store.project_agent("sw", agent)
    ended = {**asdict(agent), "input_prompt": "ended execution history " * 1000}
    archive = {f"old-{index}": {**ended, "execution_id": f"old-{index}"} for index in range(archive_size)}
    fencing.write(store, "sw", lambda doc: doc.update(executions={"active": asdict(agent)}, archive=archive))
    monkeypatch.setattr(scope, "joined", lambda redis: ["hive-a"])
    monkeypatch.setattr(scope, "member", lambda: "")
    connection_type = store.redis.connection_pool.connection_class
    original = connection_type.read_response
    transferred = []

    def reply_bytes(value: Any) -> int:
        if isinstance(value, (str, bytes)):
            return len(value)
        if isinstance(value, (list, tuple)):
            return sum(reply_bytes(part) for part in value)
        return 0

    def traced(connection: Any, *args: Any, **kwargs: Any) -> Any:
        result = original(connection, *args, **kwargs)
        transferred.append(reply_bytes(result))
        return result

    monkeypatch.setattr(connection_type, "read_response", traced)
    assert scope.readings(store.redis, "sw", agent.name).endswith(":hive:hive-a:swarm")
    assert store.agents("sw")[0].hive == "hive-a"
    assert store.execution_occupants("sw") == {"": agent}
    assert max(transferred) < 10000
    monkeypatch.setattr(connection_type, "read_response", original)
    assert fencing.read(store, "sw")["archive"] == archive


def test_a_legacy_writer_cannot_leave_stale_routing_rows(monkeypatch: pytest.MonkeyPatch) -> None:
    from fakeredis import FakeRedis

    from scripts.events import state

    store = RedisStore(FakeRedis(decode_responses=True))
    agent = AgentRecord("eng-a", "eng", "task", execution_id="active", generation=1, hive="hive-a")
    store.project_agent("sw", agent)
    fencing.write(store, "sw", lambda doc: doc.update(executions={"active": asdict(agent)}))
    assert store.agents("sw")[0].hive == "hive-a"
    states = state.for_store(store, fencing.BUCKET)
    entry = states.get("sw")
    assert entry is not None
    doc = json.loads(entry.value)
    doc["executions"]["active"]["hive"] = "hive-b"
    states.update("sw", json.dumps(doc).encode(), entry.revision)
    assert store.agents("sw")[0].hive == "hive-b"
