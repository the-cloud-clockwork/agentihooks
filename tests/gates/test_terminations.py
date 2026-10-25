import io
import json

import pytest

from scripts.gates import Call, Gate, Who, entry, modes
from scripts.gates.terminations import REFUSAL, Terminations, terminates

MASTER = Who(name="master@323133-0318", swarm="demo", lane="master")
ENGINEER = Who(name="engineer@323133-3191", swarm="demo", lane="eng", task="t1")


def rows(tmp_path):
    path = tmp_path / "demo" / "gates" / "log.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def bash(command):
    return Call("Bash", {"command": command})


def test_it_is_a_gate_that_ships_log_only():
    gate = Terminations()
    assert isinstance(gate, Gate)
    assert (gate.name, gate.default_mode) == ("terminations", "observe")
    assert isinstance(entry.HOST_GATES["terminations"], Terminations)


def test_terminating_commands_are_recognised():
    assert terminates("agentihooks terminate-agent engineer-a")
    assert terminates("cd /x && agentihooks terminate-agent 4242 --type codex")
    assert terminates("pkill -f claude")
    assert terminates("killall codex")


def test_listing_dry_runs_and_other_processes_are_not_terminations():
    assert not terminates("agentihooks terminate-agent --list --type any")
    assert not terminates("agentihooks terminate-agent engineer-a --dry-run")
    assert not terminates("pkill -f ledger_server")
    assert not terminates("echo terminate-agent")
    assert not terminates("kill -9 4242")
    assert not terminates("AGENT=claude")


def test_a_master_termination_is_a_would_be_deny_and_others_pass():
    gate = Terminations()
    call = bash("agentihooks terminate-agent engineer-a")
    assert gate.matches(call)
    assert not gate.matches(Call("Read", {}))
    decision = gate.decide(call, MASTER, None)
    assert (decision.allowed, decision.reason) == (False, REFUSAL)
    assert gate.decide(call, ENGINEER, None).allowed
    assert gate.decide(bash("agentihooks terminate-agent --list"), MASTER, None).allowed


@pytest.mark.parametrize("command", ["pkill -f codex", "agentihooks terminate-agent engineer-a"])
def test_a_master_termination_is_logged_and_let_through_in_log_only_mode(tmp_path, monkeypatch, command):
    monkeypatch.setattr(modes, "swarm_gates", lambda swarm, environ: {})
    environ = {"AGENTIHOOKS_SWARM": "demo", "AGENTIHOOKS_AGENT_NAME": MASTER.name}
    payload = json.dumps({"tool_name": "Bash", "tool_input": {"command": command}})
    assert entry.main(["terminations"], io.StringIO(payload), environ, tmp_path) == 0
    assert [(r["gate"], r["kind"], r["reason"]) for r in rows(tmp_path)] == [("terminations", "observe", REFUSAL)]
