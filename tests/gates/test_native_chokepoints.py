from unittest.mock import Mock

import pytest

from scripts import terminate_agent
from scripts.gates import entry, log
from scripts.gates.base import Who
from tests.test_terminate_agent import session


@pytest.fixture
def native_gate(monkeypatch, tmp_path):
    who = Who(name="master@123456-0001", swarm="proof", task="native")
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


@pytest.mark.parametrize("provider", ["claude", "codex"])
@pytest.mark.parametrize("mode,code,calls", [("enforce", 2, 0), ("observe", 17, 1), ("off", 17, 1)])
def test_native_authentication_is_gated(monkeypatch, native_gate, provider, mode, code, calls):
    from scripts import claude_login, codex_login

    monkeypatch.setenv("AGENTIHOOKS_GATE_SIGN_INS", "off")
    module = claude_login if provider == "claude" else codex_login
    login = Mock(return_value=(17, {}))
    monkeypatch.setattr(module, "_capture" if provider == "claude" else "_login", login)
    assert module.main(["proof", "--no-openbao"], environ={"AGENTIHOOKS_GATE_SIGN_INS": mode}) == code
    assert login.call_count == calls
    rows = log.recent("proof", home=native_gate)
    assert [(row["gate"], row["kind"]) for row in rows] == (
        [] if mode == "off" else [("sign-ins", "deny" if mode == "enforce" else "observe")]
    )


@pytest.mark.parametrize("mode,code,calls", [("enforce", 2, 0), ("observe", 0, 1), ("off", 0, 1)])
def test_hive_create_runs_node_cap_gate_before_creation(monkeypatch, native_gate, mode, code, calls):
    from scripts.hive import cli

    monkeypatch.setenv("AGENTIHOOKS_GATE_NODE_CAPS", mode)
    create = Mock(return_value="created")
    monkeypatch.setattr(cli, "_create", create)
    assert cli.main(["create", "--dry-run"]) == code
    assert create.call_count == calls
    rows = log.recent("proof", home=native_gate)
    assert [(row["gate"], row["kind"]) for row in rows] == (
        [] if mode == "off" else [("node-caps", "deny" if mode == "enforce" else "observe")]
    )


@pytest.mark.parametrize("auth,denied", [("interactive", True), ("", False)])
def test_only_explicit_auth_launches_run_sign_in_gate(monkeypatch, native_gate, capsys, auth, denied):
    from scripts import init_agent

    monkeypatch.setenv("AGENTIHOOKS_GATE_SIGN_INS", "off")
    resolve = Mock(side_effect=ValueError("stop before launch"))
    monkeypatch.setattr(init_agent, "_resolve_directory", resolve)
    monkeypatch.setattr(init_agent.operator_env, "fill", lambda env: None)
    args = ["--dry-run", *(["--auth", auth] if auth else [])]
    assert init_agent.main(args, {"AGENTIHOOKS_GATE_SIGN_INS": "enforce"}) == 2
    assert resolve.call_count == (0 if denied else 1)
    if denied:
        assert (
            "agentihooks init-agent: explicit authentication refused by the sign ins gate\n" in capsys.readouterr().err
        )
    assert [row["gate"] for row in log.recent("proof", home=native_gate)] == (["sign-ins"] if denied else [])


@pytest.mark.parametrize(
    "name,operation", [("terminations", "terminate-agent"), ("sign-ins", "claude-login"), ("node-caps", "hive-create")]
)
@pytest.mark.parametrize("key", ["CLAUDE_CODE_SESSION_ID", "CODEX_THREAD_ID"])
def test_native_calls_honor_only_the_callers_session_lift(native_gate, name, operation, key):
    from scripts.gates import lift, modes

    lift.arm("proof", "lifted-session", name, native_gate)
    env = {key: "lifted-session", modes.env_name(name): "enforce"}
    assert entry.native(name, operation, env) == 0
    env[key] = "another-session"
    assert entry.native(name, operation, env) == 2
    rows = log.recent("proof", home=native_gate)
    assert [row["kind"] for row in rows] == ["observe", "deny"]
    assert rows[0]["reason"].startswith(entry.LIFTED)


@pytest.mark.parametrize("operation", ["claude-login", "codex-login", "init-agent-auth"])
def test_native_authentication_events_match_and_explain_the_refusal(operation):
    from scripts.gates.base import Call
    from scripts.gates.sign_ins import SignIns

    gate = SignIns()
    call = Call(operation)
    assert gate.matches(call)
    denied = gate.decide(call, Who(name="engineer@123456-0001"), None)
    assert not denied.allowed
    assert denied.reason == "sign in: agents never initiate authentication; send the step to the front door master."
    assert gate.decide(call, Who(), None).allowed


def test_native_creation_event_matches_and_explains_the_refusal():
    from scripts.gates.base import Call
    from scripts.gates.node_caps import NodeCaps, refusal

    gate = NodeCaps()
    call = Call("hive-create")
    assert gate.matches(call)
    denied = gate.decide(call, Who(name="engineer@123456-0001"), None)
    assert not denied.allowed
    assert denied.reason == refusal(["hive create adds infrastructure capacity"])
    assert gate.decide(call, Who(), None).allowed


def test_native_termination_event_matches():
    from scripts.gates.base import Call
    from scripts.gates.terminations import Terminations

    assert Terminations().matches(Call("terminate-agent"))
