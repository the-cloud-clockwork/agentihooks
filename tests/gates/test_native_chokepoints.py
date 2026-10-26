from unittest.mock import Mock

import pytest

from scripts import terminate_agent
from scripts.gates import entry, log
from scripts.gates.base import Who
from tests.test_terminate_agent import session


@pytest.fixture
def native_gate(monkeypatch, tmp_path):
    who = Who(name="master@proof", swarm="proof", task="native")
    monkeypatch.setattr(entry.Who, "from_env", lambda environ=None: who)
    monkeypatch.setattr(log, "swarm_home", lambda: tmp_path)
    monkeypatch.setattr(entry.modes, "swarm_gates", lambda *args: None)
    return tmp_path


@pytest.mark.parametrize("mode,code,signals", [("enforce", 2, 0), ("observe", 0, 1), ("off", 0, 1)])
def test_native_termination_runs_gate_before_signals(monkeypatch, native_gate, mode, code, signals):
    monkeypatch.setenv("AGENTIHOOKS_GATE_TERMINATIONS", mode)
    item = session()
    monkeypatch.setattr(terminate_agent, "sessions", lambda: [item])
    monkeypatch.setattr(terminate_agent, "validate", lambda *args, **kwargs: [item.process])
    monkeypatch.setattr(terminate_agent, "herdr_pane", lambda pid: ("", ""))
    monkeypatch.setattr(terminate_agent, "_mark_exit", lambda pid: ("", ""))
    stop = Mock(return_value=False)
    monkeypatch.setattr(terminate_agent, "terminate", stop)

    assert terminate_agent.main(["uuid-1"]) == code
    assert stop.call_count == signals
    rows = log.recent("proof", home=native_gate)
    assert [(row["gate"], row["kind"]) for row in rows] == (
        [] if mode == "off" else [("terminations", "deny" if mode == "enforce" else "observe")]
    )


def test_native_termination_dry_run_remains_exempt(monkeypatch, native_gate):
    monkeypatch.setenv("AGENTIHOOKS_GATE_TERMINATIONS", "enforce")
    item = session()
    monkeypatch.setattr(terminate_agent, "sessions", lambda: [item])
    monkeypatch.setattr(terminate_agent, "validate", lambda *args, **kwargs: [item.process])
    monkeypatch.setattr(terminate_agent, "herdr_pane", lambda pid: ("", ""))
    assert terminate_agent.main(["uuid-1", "--dry-run"]) == 0
    assert log.recent("proof", home=native_gate) == []
