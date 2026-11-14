from typing import Any

import pytest

from scripts.swarm import ledger_client
from scripts.swarm.store import RedisStore, SwarmConfig
from scripts.swarm.tick import tick
from tests.swarm.test_tick import FakeRuntime, tasks


@pytest.fixture
def store() -> RedisStore:
    import fakeredis

    found = RedisStore(fakeredis.FakeRedis(decode_responses=True))
    found.create(SwarmConfig("sw", "/repo", max_eng=2, max_ci=1))
    return found


@pytest.mark.feature("tick-time-left-server-error")
def test_a_ledger_server_error_on_the_time_left_write_keeps_the_pass_running(
    store: RedisStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    def refused(slug: str, ops: Any = None, service: bool = False) -> dict[str, Any]:
        raise SystemExit("server refused: 500 storage_error")

    monkeypatch.setattr(ledger_client._ledger(), "call", refused)
    ledger: Any = tasks(("t1", "eng"))
    runtime = FakeRuntime()
    ledger.time_left = ledger_client.LedgerClient(service=True).time_left
    actions = tick("sw", store, ledger, runtime, now_ms=1_000)
    assert "spawned engineer@a1b2c3-0001 for t1" in actions
    assert "skipped the time left write: ledger sw: server refused: 500 storage_error" in actions
    assert runtime.spawned == [("eng", "engineer@a1b2c3-0001", "t1")]


@pytest.mark.feature("tick-time-left-server-error")
def test_under_events_a_ledger_server_error_on_the_time_left_write_keeps_the_pass_reaping(
    store: RedisStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    from scripts.swarm.store import AgentRecord

    def refused(slug: str, ops: Any = None, service: bool = False) -> dict[str, Any]:
        raise SystemExit("server refused: 500 storage_error")

    monkeypatch.setattr(ledger_client._ledger(), "call", refused)
    store.update("sw", engine="events")
    ledger: Any = tasks(("t1", "eng"))
    ledger.rows["t1"].update(state="claimed", claimed_by="engineer@a1b2c3-0009")
    store.put_agent("sw", AgentRecord("engineer@a1b2c3-0009", "eng", "t1", started_at=1_000))
    runtime = FakeRuntime()
    ledger.time_left = ledger_client.LedgerClient(service=True).time_left
    actions = tick("sw", store, ledger, runtime, now_ms=10_000_000)
    assert "skipped the time left write: ledger sw: server refused: 500 storage_error" in actions
    assert [action for action in actions if action.startswith("lost engineer@a1b2c3-0009")]
    assert runtime.spawned == []
