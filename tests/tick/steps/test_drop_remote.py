from typing import Any

import fakeredis
import pytest

from scripts.swarm import fencing, tick
from scripts.swarm.store import AgentRecord, RedisStore, SwarmConfig
from tests.swarm.test_tick import FakeLedger


@pytest.mark.feature("swarm-tick-dead-remote-budget")
def test_ten_dead_remote_seats_drop_without_loading_the_execution_archive(monkeypatch: pytest.MonkeyPatch) -> None:
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
