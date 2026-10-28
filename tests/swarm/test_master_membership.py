from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from scripts.swarm import ledger_client
from scripts.swarm.store import AgentRecord, SwarmConfig
from scripts.swarm_v2.runtime.base import Outcome, Status
from scripts.swarm_v2.runtime.routed import RoutedRuntime


@pytest.mark.parametrize("seat", ["master@sw", "master-2@sw"])
@pytest.mark.parametrize("handoff", ["", "Successor continuity"])
@pytest.mark.parametrize("remote", [False, True])
def test_master_joins_as_orchestrator_before_dispatch(monkeypatch, seat, handoff, remote):
    ledger = Mock()
    monkeypatch.setattr(ledger_client, "LedgerClient", Mock(return_value=ledger))
    config = SwarmConfig("sw", "/repo")
    name = "master@a1b2c3-0002"

    def dispatch(request):
        ledger.join.assert_called_once_with("sw", name, "orchestrator")
        assert request.task == {"id": "master", "seat": seat, "handoff": handoff}
        return Outcome("spawn", Status.OK, "local", "placed")

    router = SimpleNamespace(spawn=dispatch, spawn_backend=lambda request: "kubernetes")
    runtime = RoutedRuntime(Mock(), router, launch=dispatch if remote else None)
    assert runtime.spawn(config, "master", name, {"id": "master", "seat": seat, "handoff": handoff}) == "placed"


def test_master_resume_joins_before_recovery(monkeypatch):
    ledger = Mock()
    monkeypatch.setattr(ledger_client, "LedgerClient", Mock(return_value=ledger))
    agent = AgentRecord("master@a1b2c3-0002", "master", "master", seat="master@sw")

    def recover(*args):
        ledger.join.assert_called_once_with("sw", agent.name, "orchestrator")
        return Outcome("recover", Status.OK, "local", "resumed")

    runtime = RoutedRuntime(Mock(), SimpleNamespace(recover=recover))
    assert runtime.resume(SwarmConfig("sw", "/repo"), agent, "Continue") == "resumed"


def test_refused_master_join_starts_no_process(monkeypatch):
    ledger = Mock()
    ledger.join.side_effect = ledger_client.LedgerRefused("membership refused")
    monkeypatch.setattr(ledger_client, "LedgerClient", Mock(return_value=ledger))
    router = Mock()
    runtime = RoutedRuntime(Mock(), router)
    with pytest.raises(ledger_client.LedgerRefused, match="membership refused"):
        runtime.spawn(SwarmConfig("sw", "/repo"), "master", "master@a1b2c3-0002", {"id": "master"})
    router.spawn.assert_not_called()


def test_worker_dispatch_does_not_request_orchestrator_membership(monkeypatch):
    client = Mock()
    monkeypatch.setattr(ledger_client, "LedgerClient", client)
    router = Mock()
    router.spawn.return_value = Outcome("spawn", Status.OK, "local", "placed")
    runtime = RoutedRuntime(Mock(), router)
    assert runtime.spawn(SwarmConfig("sw", "/repo"), "eng", "engineer@a1b2c3-0002", {"id": "task"}) == "placed"
    client.assert_not_called()
