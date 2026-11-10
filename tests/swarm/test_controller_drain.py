"""The controller finishes its running tick pass on the stop signal the runtime sends, then exits."""

import signal
import threading
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import yaml

from scripts import operator_env
from scripts.swarm import controller, tick_stop
from scripts.swarm.ledger_client import LedgerUnreachable
from scripts.swarm_v2 import control_service, proof_swarm

pytestmark = pytest.mark.unit

CHART = Path(__file__).resolve().parents[2] / "deploy/helm/agentihooks-swarm"
GOLDEN_PASS_S = 100


class Service:
    """A control service that counts its ticks and stops."""

    def __init__(self) -> None:
        self.runtime = None
        self.controller = SimpleNamespace(require=lambda *args: None)
        self.ticks = 0
        self.stopped = 0

    def tick(self) -> None:
        self.ticks += 1

    def stop(self) -> None:
        self.stopped += 1


class Rig:
    """The controller run with every collaborator faked and every signal handler recorded."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.installed: list[tuple[int, Any]] = []
        self.handlers: dict[int, Any] = {}
        self.events: list[str] = []
        self.service = Service()
        self.startup: Callable[[], None] = lambda: None
        monkeypatch.setattr(tick_stop, "STOP", threading.Event())
        monkeypatch.setattr(controller, "RUNNING", threading.Event(), raising=False)
        monkeypatch.setattr(controller, "DRAINING", threading.Event(), raising=False)
        monkeypatch.setattr(tick_stop, "_HELD", {})
        monkeypatch.setattr(operator_env, "fill", lambda env: None)
        monkeypatch.setattr(operator_env, "classifier", lambda env: [])
        monkeypatch.setattr(controller, "connect", lambda: "store")
        monkeypatch.setattr(controller, "scheduled_wait", lambda seconds: self.events.append("wait"))
        monkeypatch.setattr(proof_swarm, "run", self.proofs)
        monkeypatch.setattr(control_service, "host", lambda env, store, hive: self.service)
        monkeypatch.setattr(signal, "signal", self.install)

    def install(self, number: int, handler: Any) -> str:
        self.installed.append((number, handler))
        self.handlers.setdefault(number, handler)
        return f"previous {int(number)}"

    def proofs(self, *args: Any) -> None:
        if len(args) == 2:
            self.startup()
            return
        self.events.append("proofs")

    def deliver(self, number: int) -> None:
        self.handlers[number](number, None)

    def run(self) -> int | str | None:
        with pytest.raises(SystemExit) as stopped:
            controller.main(["run"])
        return stopped.value.code


@pytest.fixture
def rig(monkeypatch: pytest.MonkeyPatch) -> Rig:
    return Rig(monkeypatch)


@pytest.mark.feature("controller-drains-pass-on-stop-signal")
def test_the_interrupt_and_terminate_signals_share_one_stop_handler_restored_on_exit(
    rig: Rig, monkeypatch: pytest.MonkeyPatch
) -> None:
    def tick_once(store: str, service: Service) -> dict:
        rig.deliver(signal.SIGINT)
        return {}

    monkeypatch.setattr(controller, "tick_once", tick_once)
    rig.run()

    numbers = [number for number, _ in rig.installed[:2]]
    assert sorted(numbers) == sorted([signal.SIGINT, signal.SIGTERM])
    assert rig.installed[0][1] is rig.installed[1][1]
    assert sorted(rig.installed[2:]) == sorted(
        [(signal.SIGINT, f"previous {int(signal.SIGINT)}"), (signal.SIGTERM, f"previous {int(signal.SIGTERM)}")]
    )


@pytest.mark.parametrize("number", [signal.SIGINT, signal.SIGTERM])
@pytest.mark.feature("controller-drains-pass-on-stop-signal")
def test_a_stop_signal_during_a_pass_lets_the_pass_finish_then_exits(
    rig: Rig, monkeypatch: pytest.MonkeyPatch, number: int
) -> None:
    released: list[str] = []

    def tick_once(store: str, service: Service) -> dict:
        tick_stop.hold("lock", lambda: released.append("lock"))
        rig.deliver(number)
        rig.events.append("tick finished")
        assert not tick_stop.STOP.is_set()
        tick_stop.check()
        return {"swarm": ["spawned"]}

    monkeypatch.setattr(controller, "tick_once", tick_once)

    assert rig.run() == 143
    assert rig.events == ["tick finished", "proofs"]
    assert rig.service.ticks == 1
    assert rig.service.stopped == 1
    assert released == ["lock"]
    assert tick_stop._HELD == {}


@pytest.mark.feature("controller-drains-pass-on-stop-signal")
def test_a_stop_signal_between_passes_exits_at_once(rig: Rig, monkeypatch: pytest.MonkeyPatch) -> None:
    released: list[str] = []
    ticks: list[str] = []

    def startup() -> None:
        tick_stop.hold("lock", lambda: released.append("lock"))
        rig.deliver(signal.SIGINT)

    rig.startup = startup
    monkeypatch.setattr(controller, "tick_once", lambda store, service: ticks.append("tick") or {})

    assert rig.run() == 143
    assert ticks == []
    assert released == ["lock"]
    assert tick_stop.STOP.is_set()


@pytest.mark.feature("controller-drains-pass-on-stop-signal")
def test_a_pass_that_fails_after_a_stop_signal_exits_without_waiting_for_another(
    rig: Rig, monkeypatch: pytest.MonkeyPatch, capfd: pytest.CaptureFixture[str]
) -> None:
    passes: list[str] = []

    def tick_once(store: str, service: Service) -> dict:
        passes.append("pass")
        rig.deliver(signal.SIGINT)
        raise LedgerUnreachable("ledger gone")

    monkeypatch.setattr(controller, "tick_once", tick_once)

    assert rig.run() == 143
    assert passes == ["pass"]
    assert "wait" not in rig.events
    assert capfd.readouterr().err.splitlines() == [
        f"controller: stop signal {int(signal.SIGINT)}, finishing the running pass before exit",
        "controller: the last pass before exit ended: ledger gone",
    ]


@pytest.mark.feature("controller-drains-pass-on-stop-signal")
def test_a_second_stop_signal_during_a_pass_exits_at_once(rig: Rig, monkeypatch: pytest.MonkeyPatch) -> None:
    def tick_once(store: str, service: Service) -> dict:
        rig.deliver(signal.SIGINT)
        rig.deliver(signal.SIGTERM)
        rig.events.append("tick finished")
        return {}

    monkeypatch.setattr(controller, "tick_once", tick_once)

    assert rig.run() == 143
    assert rig.events == []
    assert tick_stop.STOP.is_set()


@pytest.mark.feature("controller-drains-pass-on-stop-signal")
def test_the_controller_grace_period_outlasts_the_golden_pass_with_margin() -> None:
    values = yaml.safe_load((CHART / "values.yaml").read_text())["controller"]
    template = (CHART / "templates/controller.yaml").read_text()

    assert values["terminationGracePeriodSeconds"] >= 2 * GOLDEN_PASS_S
    assert (
        "terminationGracePeriodSeconds: {{ .Values.controller.terminationGracePeriodSeconds | default 300 }}"
        in template
    )
