import time
from pathlib import Path
from typing import Any

import fakeredis
import pytest

from scripts.swarm import fencing, tick
from scripts.swarm.store import AgentRecord, RedisStore, SwarmConfig
from tests.swarm.test_tick import FakeLedger


@pytest.mark.feature("swarm-tick-dead-remote-budget")
@pytest.mark.parametrize("entry", ["tick", "run_tick"])
def test_a_hive_roll_drops_ten_seats_and_launches_within_two_budgeted_passes(
    entry: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from tests.swarm.tick_scenarios.test_characterisation import agent_steps, replay

    dropped: list[float] = []
    reaped: list[float] = []
    drop, reap = tick._drop_remote, agent_steps._reap

    def measured_drop(*args: Any, **kwargs: Any) -> str:
        started = time.monotonic()
        action: str = drop(*args, **kwargs)
        dropped.append(time.monotonic() - started)
        return action

    def measured_reap(*args: Any, **kwargs: Any) -> list[str]:
        started = time.monotonic()
        actions: list[str] = reap(*args, **kwargs)
        reaped.append(time.monotonic() - started)
        return actions

    monkeypatch.setattr(tick, "_drop_remote", measured_drop)
    monkeypatch.setattr(agent_steps, "_reap", measured_reap)
    record = replay("hive-roll", entry, monkeypatch, tmp_path)
    assert len(dropped) == 10
    assert max(dropped) < 5.0
    assert len(reaped) == 2
    assert max(reaped) < 22.0
    assert any(action.startswith("spawned engineer") for actions in record["actions"][:2] for action in actions)


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
