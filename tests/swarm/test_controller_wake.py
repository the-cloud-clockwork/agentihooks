"""A wake signal starts the controller's next pass at once instead of at the end of its tick."""

import signal
from collections.abc import Callable
from typing import Any

import pytest

from scripts import operator_env
from scripts.scheduler import process_timer
from scripts.swarm import controller
from scripts.swarm_v2 import control_service, proof_swarm

pytestmark = pytest.mark.unit


@pytest.mark.feature("controller-wake-signal-runs-next-pass")
def test_a_wake_signal_ends_the_next_tick_wait_once(monkeypatch: pytest.MonkeyPatch) -> None:
    waits: list[tuple[float, bool]] = []

    class Timer:
        def __init__(self, delay: float, *, state: object = None, woken: Callable[[], bool] = bool) -> None:
            self.delay, self.woken = delay, woken

        def __enter__(self) -> "Timer":
            return self

        def __exit__(self, *args: object) -> None:
            return None

        def wait(self, timeout: float | None = None) -> None:
            waits.append((self.delay, self.woken()))

    monkeypatch.setattr(process_timer, "Timer", Timer)
    monkeypatch.setattr(controller, "WAKES", [])
    controller._woken(signal.SIGUSR1, None)
    controller._tick_wait(120)
    controller._tick_wait(120)
    assert waits == [(120, True), (120, False)]


@pytest.mark.feature("controller-wake-signal-runs-next-pass")
def test_the_controller_run_holds_the_wake_handler_until_it_exits(monkeypatch: pytest.MonkeyPatch) -> None:
    installed: list[tuple[int, Any]] = []
    held: list[bool] = []
    monkeypatch.setattr(operator_env, "fill", lambda env: None)
    monkeypatch.setattr(operator_env, "classifier", lambda env: [])
    monkeypatch.setattr(controller, "connect", lambda: "store")
    monkeypatch.setattr(proof_swarm, "run", lambda *args: None)
    monkeypatch.setattr(control_service, "host", lambda env, store, hive: None)
    monkeypatch.setattr(controller, "tick_once", lambda store, service: {})
    monkeypatch.setattr(signal, "signal", lambda number, handler: installed.append((number, handler)) or "previous")

    def wait(seconds: float) -> None:
        held.append(installed.count((signal.SIGUSR1, controller._woken)) == 1)
        raise KeyboardInterrupt

    monkeypatch.setattr(controller, "scheduled_wait", wait)
    with pytest.raises(KeyboardInterrupt):
        controller.main(["run"])
    assert held == [True]
    assert installed[-1] == (signal.SIGUSR1, "previous")
