"""The controller run loop survives a pass whose ledger client times out."""

import threading
from types import SimpleNamespace

import pytest

from scripts import operator_env
from scripts.swarm import controller, ledger_client, tick_stop
from scripts.swarm.ledger_client import LedgerUnreachable
from scripts.swarm_v2 import control_service, proof_swarm

TIMEOUT = "ledger rig-grade-swarm: ledger server not answering on http://ledger:8765: The read operation timed out"


class Ended(Exception):
    """Raised by the fake pass to end the run loop once the test has seen enough passes."""


class Service:
    """A control service double that counts its stops and ticks."""

    def __init__(self) -> None:
        self.stopped = 0
        self.ticks = 0

    def stop(self) -> None:
        self.stopped += 1

    def tick(self) -> None:
        self.ticks += 1


@pytest.fixture
def service(monkeypatch: pytest.MonkeyPatch) -> Service:
    running = Service()
    monkeypatch.setattr(tick_stop, "STOP", threading.Event())
    monkeypatch.setattr(operator_env, "fill", lambda env: None)
    monkeypatch.setattr(operator_env, "classifier", lambda env: [])
    monkeypatch.setattr(controller, "connect", lambda: "store")
    monkeypatch.setattr(controller, "open_controller_bus", lambda env: None)
    monkeypatch.setattr(proof_swarm, "run", lambda *args: None)
    monkeypatch.setattr(control_service, "host", lambda env, store, hive: running)
    return running


@pytest.mark.feature("controller-survives-ledger-timeout")
def test_controller_run_backs_off_after_a_ledger_timeout_and_runs_the_next_pass(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], service: Service
) -> None:
    passes: list[int] = []
    waits: list[float] = []
    monkeypatch.setattr(controller, "scheduled_wait", waits.append)

    def timed_out(slug: str, ops: list | None, service: bool) -> dict:
        raise SystemExit("ledger server not answering on http://ledger:8765: The read operation timed out")

    fake = SimpleNamespace(call=timed_out, Missing=LookupError)
    monkeypatch.setattr(ledger_client, "_ledger", lambda: fake)

    def tick_once(store: str, running: Service) -> dict:
        passes.append(len(passes) + 1)
        if len(passes) == 1:
            return ledger_client.LedgerClient(service=True)._call("rig-grade-swarm")
        raise Ended

    monkeypatch.setattr(controller, "tick_once", tick_once)
    with pytest.raises(Ended):
        controller.main(["run"])
    assert passes == [1, 2]
    assert waits and waits[0] > 0
    assert "ledger rig-grade-swarm: ledger server not answering" in capsys.readouterr().err
    assert service.stopped == 1


@pytest.mark.feature("controller-survives-ledger-timeout")
def test_controller_run_once_reports_a_ledger_timeout_as_a_failed_pass(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], service: Service
) -> None:
    def tick_once(store: str, running: Service) -> dict:
        raise LedgerUnreachable(TIMEOUT)

    monkeypatch.setattr(controller, "tick_once", tick_once)
    monkeypatch.setattr(controller, "scheduled_wait", lambda seconds: pytest.fail("a single pass never waits"))
    assert controller.main(["run", "--once"]) == 1
    assert "The read operation timed out" in capsys.readouterr().err
    assert service.stopped == 1
