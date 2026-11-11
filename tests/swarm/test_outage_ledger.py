import urllib.error
from pathlib import Path
from typing import Any
from unittest.mock import Mock

import pytest

from scripts.hive import sweep
from scripts.swarm import cli, command_runner, delivery, incidents, ledger_watchdog, outage
from scripts.swarm.health import prompt_backlog
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


@pytest.mark.feature("swarm-tick-redis-only-steps")
def test_a_ledger_down_without_a_stored_view_still_runs_the_redis_only_steps(
    store: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def refused(slug: str, service: bool = False, timeout: float | None = None) -> None:
        raise urllib.error.URLError("refused")

    called: list[str] = []
    monkeypatch.setattr(outage, "HOME", tmp_path)
    monkeypatch.setattr(cli.ledger_view, "start", refused)
    monkeypatch.setattr(cli.ledger_view, "hold_stored", lambda slug: None)
    monkeypatch.setattr(cli.ledger_view, "stored", lambda slug: None)
    monkeypatch.setattr(LedgerClient, "metadata", Mock(return_value=None))
    monkeypatch.setattr(incidents, "host_pressure", lambda *args, **kwargs: called.append("host pressure") or [])
    monkeypatch.setattr(prompt_backlog, "observe", lambda *args, **kwargs: called.append("prompt backlog") or [])
    monkeypatch.setattr(sweep, "sweep", lambda *args, **kwargs: called.append("hive sweep") or [])
    monkeypatch.setattr(delivery, "migrate_outbox", lambda *args, **kwargs: called.append("outbox migration") or 0)
    monkeypatch.setattr(ledger_watchdog, "watch", lambda *args, **kwargs: called.append("ledger watchdog") or [])
    monkeypatch.setattr(command_runner, "consume", lambda *args, **kwargs: called.append("command runner") or [])
    assert cli.run_tick(store, "sw", LedgerClient(), FakeRuntime()) == ["Ledger health probe failed"]
    assert sorted(called) == ["hive sweep", "host pressure", "outbox migration", "prompt backlog"]
