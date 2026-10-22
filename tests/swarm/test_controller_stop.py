import signal

import pytest

from scripts import operator_env
from scripts.swarm import controller
from scripts.swarm_v2 import control_service, proof_swarm


def test_controller_run_turns_sigterm_into_exit_so_a_held_tick_unwinds(monkeypatch):
    installed, unwound = [], []
    monkeypatch.setattr(operator_env, "fill", lambda env: None)
    monkeypatch.setattr(operator_env, "classifier", lambda env: [])
    monkeypatch.setattr(controller, "connect", lambda: "store")
    monkeypatch.setattr(proof_swarm, "run", lambda *args: None)
    monkeypatch.setattr(control_service, "host", lambda env, store, hive: None)
    monkeypatch.setattr(signal, "signal", lambda number, handler: installed.append((number, handler)) or "previous")

    def tick_once(store, service):
        assert (store, service) == ("store", None)
        number, handler = installed[0]
        assert number == signal.SIGTERM
        try:
            handler(number, None)
        finally:
            unwound.append("tick lock released")

    monkeypatch.setattr(controller, "tick_once", tick_once)
    with pytest.raises(SystemExit) as stopped:
        controller.main(["run"])
    assert stopped.value.code == 143
    assert unwound == ["tick lock released"]
    assert installed[1:] == [(signal.SIGTERM, "previous")]
