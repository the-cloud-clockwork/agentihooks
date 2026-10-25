from unittest.mock import Mock

import pytest

from scripts.swarm import cli, outage
from scripts.swarm.ledger_client import LedgerClient
from scripts.swarm.store import AgentRecord
from tests.swarm.test_tick import FakeRuntime, tasks
from tests.swarm.test_tick import store as store

pytestmark = pytest.mark.unit


@pytest.mark.parametrize("response", [None, ""])
def test_tick_records_a_failed_ledger_health_probe_and_closes_it_when_health_returns(
    store, tmp_path, monkeypatch, response
):
    monkeypatch.setattr(outage, "HOME", tmp_path)
    monkeypatch.setattr(cli, "now_ms", lambda: 1000)
    probe = Mock(return_value=response)
    monkeypatch.setattr(LedgerClient, "metadata", probe)
    assert cli.run_tick(store, "sw", LedgerClient(), FakeRuntime()) == ["Ledger health probe failed"]
    probe.assert_called_once_with("sw")
    assert outage.read("sw")["ledger"] == {
        "kind": "ledger",
        "started_at": 1000,
        "evidence": ["Ledger health probe failed"],
        "ended_at": None,
    }
    monkeypatch.setattr(cli, "now_ms", lambda: 2000)
    probe.return_value = {}
    assert outage.ledger_health("sw", 2000, lambda: isinstance(LedgerClient().metadata("sw"), dict))
    assert outage.read("sw")["ledger"]["ended_at"] == 2000


def test_tick_records_the_provider_error_observed_for_its_agent(store, tmp_path, monkeypatch):
    from scripts.swarm.pane import PaneObservation

    monkeypatch.setattr(outage, "HOME", tmp_path)
    agent = AgentRecord("worker", "eng", "t1", harness="codex")
    store.put_agent("sw", agent)
    runtime = FakeRuntime()
    monkeypatch.setattr(
        runtime,
        "observe",
        lambda agent: PaneObservation(
            "idle", provider_error="stream disconnected before completion", provider_checked=True
        ),
    )
    cli.run_tick(store, "sw", tasks(), runtime)
    assert outage.open_records("sw")[0]["evidence"] == [
        "worker (codex, account unknown): stream disconnected before completion"
    ]
