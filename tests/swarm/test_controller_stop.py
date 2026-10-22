import signal
import threading

import pytest

from scripts import operator_env
from scripts.swarm import controller, tick_stop
from scripts.swarm_v2 import control_service, proof_swarm


@pytest.fixture(autouse=True)
def stop(monkeypatch):
    monkeypatch.setattr(tick_stop, "STOP", threading.Event())


class Service:
    def __init__(self):
        self.stopped = 0

    def stop(self):
        self.stopped += 1


def test_controller_run_turns_sigterm_into_exit_so_its_cleanup_runs(monkeypatch):
    installed, service = [], Service()
    monkeypatch.setattr(operator_env, "fill", lambda env: None)
    monkeypatch.setattr(operator_env, "classifier", lambda env: [])
    monkeypatch.setattr(controller, "connect", lambda: "store")
    monkeypatch.setattr(proof_swarm, "run", lambda env, store: None)
    monkeypatch.setattr(control_service, "host", lambda env, store, hive: service if store == "store" else None)
    monkeypatch.setattr(signal, "signal", lambda number, handler: installed.append((number, handler)) or "previous")

    def tick_once(store, running):
        assert (store, running) == ("store", service)
        number, handler = installed[0]
        assert number == signal.SIGTERM
        handler(number, None)

    monkeypatch.setattr(controller, "tick_once", tick_once)
    with pytest.raises(SystemExit) as stopped:
        controller.main(["run"])
    assert stopped.value.code == 143
    assert service.stopped == 1
    assert installed[1:] == [(signal.SIGTERM, "previous")]


def test_controller_run_releases_held_tick_locks_on_sigterm_before_exiting(monkeypatch):
    installed, released = [], []
    monkeypatch.setattr(tick_stop, "_HELD", {})
    monkeypatch.setattr(operator_env, "fill", lambda env: None)
    monkeypatch.setattr(operator_env, "classifier", lambda env: [])
    monkeypatch.setattr(controller, "connect", lambda: "store")
    monkeypatch.setattr(proof_swarm, "run", lambda env, store: None)
    monkeypatch.setattr(control_service, "host", lambda env, store, hive: Service())
    monkeypatch.setattr(signal, "signal", lambda number, handler: installed.append((number, handler)) or "previous")

    def tick_once(store, running):
        tick_stop.hold("token", lambda: released.append("token"))
        number, handler = installed[0]
        handler(number, None)

    monkeypatch.setattr(controller, "tick_once", tick_once)
    with pytest.raises(SystemExit) as stopped:
        controller.main(["run"])
    assert stopped.value.code == 143
    assert released == ["token"]
    assert tick_stop._HELD == {}
    with pytest.raises(tick_stop.Stopped):
        tick_stop.check()
