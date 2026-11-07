"""The controller run loop survives a pass whose ledger client times out."""

import threading

import pytest

from scripts import operator_env
from scripts.swarm import controller, ledger_client, tick_stop
from scripts.swarm.ledger_client import LedgerUnreachable
from scripts.swarm_v2 import control_service, proof_swarm

TIMEOUT = "ledger rig-grade-swarm: ledger server not answering on http://ledger:8765: The read operation timed out"


class TimedOutLedger:
    """A ledger module double whose server read times out the way the in-cluster ledger did."""

    class Missing(Exception):
        """The ledger module's missing ledger error."""

    @staticmethod
    def call(slug: str, ops: list[dict] | None, service: bool = False) -> dict:
        raise SystemExit("ledger server not answering on http://ledger:8765: The read operation timed out")


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


class Stderr:
    """A standard error double that records each write and flush in order."""

    def __init__(self) -> None:
        self.events: list[str] = []

    def write(self, text: str) -> int:
        self.events.append(text)
        return len(text)

    def flush(self) -> None:
        self.events.append("<flush>")


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
    monkeypatch: pytest.MonkeyPatch, service: Service
) -> None:
    passes: list[int] = []
    waits: list[float] = []
    stderr = Stderr()
    monkeypatch.setenv("AGENTIHOOKS_CONTROLLER_TICK_SECONDS", "7")
    monkeypatch.setattr(controller.sys, "stderr", stderr)
    monkeypatch.setattr(controller, "scheduled_wait", waits.append)
    monkeypatch.setattr(ledger_client, "_ledger", lambda: TimedOutLedger)

    def tick_once(store: str, running: Service) -> dict:
        passes.append(len(passes) + 1)
        if len(passes) == 1:
            ledger_client.LedgerClient(service=True).state("rig-grade-swarm")
        raise Ended

    monkeypatch.setattr(controller, "tick_once", tick_once)
    with pytest.raises(Ended):
        controller.main(["run"])
    assert passes == [1, 2]
    assert waits == [7.0]
    written = [index for index, text in enumerate(stderr.events) if "The read operation timed out" in text]
    assert written
    assert "<flush>" in stderr.events[written[0] :]
    assert service.ticks == 0
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
