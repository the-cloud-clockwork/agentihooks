import json
import subprocess
from unittest.mock import MagicMock

import pytest

from scripts.swarm import provider_probe


@pytest.mark.parametrize("harness", ["claude", "codex", "copilot"])
@pytest.mark.parametrize("model", ["", "chosen-model"])
def test_native_request_disables_tools_and_session_persistence(tmp_path, harness, model):
    account = "default" if harness == "copilot" else "alpha"
    calls = []

    def run(args, **kwargs):
        calls.append(args)
        return subprocess.CompletedProcess(args, 1, "", "")

    provider_probe.check(provider_probe.Target(harness, account, model), tmp_path, run)
    selected = ["--model", model] if model else []
    if harness == "claude":
        expected = [
            "agentihooks",
            "claude",
            "--route",
            "alpha",
            "-p",
            *selected,
            "--output-format",
            "json",
            "--tools",
            "",
            "--no-session-persistence",
            "--strict-mcp-config",
            "--mcp-config",
            '{"mcpServers":{}}',
            "--settings",
            '{"disableAllHooks":true}',
            "--system-prompt",
            "Reply with exactly OK.",
        ]
    elif harness == "codex":
        expected = [
            "agentihooks",
            "codex",
            "--route",
            "alpha",
            "exec",
            *selected,
            "--sandbox",
            "read-only",
            "--skip-git-repo-check",
            "--ephemeral",
            "--json",
            "-c",
            'model_reasoning_effort="low"',
            "-c",
            'developer_instructions="Reply with exactly OK."',
        ]
    else:
        expected = [
            "copilot",
            "-p",
            "Reply with exactly OK.",
            *selected,
            "--silent",
            "--stream",
            "off",
            "--available-tools",
            "",
            "--disable-builtin-mcps",
            "--no-custom-instructions",
            "--no-remote-export",
            "--log-level",
            "none",
        ]
    assert calls == [expected]


def test_probe_environment_keeps_account_route_and_removes_parent_identity(tmp_path, monkeypatch):
    from hooks.context.account_sessions import INTERNAL_RUN

    values = {
        "AGENTIHOOKS_SWARM": "parent",
        "AGENTIHOOKS_SWARM_TASK": "task",
        "AGENTIHOOKS_AGENT_NAME": "worker",
        "AGENTIHOOKS_PROFILE_NAME": "engineer",
        "CLAUDECODE": "parent",
        "AGENTIHOOKS_SESSION_ID": "parent",
        "PATH": "route-bin",
        "LANG": "C",
    }
    for name, value in values.items():
        monkeypatch.setenv(name, value)
    calls = []

    def run(args, **kwargs):
        child_env = kwargs.pop("env")
        names = set(child_env)
        assert names.isdisjoint(set(values) - {"PATH", "LANG"})
        safe_values = {
            name: child_env.get(name) for name in ["PATH", "LANG", "AGENTIHOOKS_CLASSIFIER_CHILD", INTERNAL_RUN]
        }
        assert safe_values == {"PATH": "route-bin", "LANG": "C", "AGENTIHOOKS_CLASSIFIER_CHILD": "1", INTERNAL_RUN: "1"}
        calls.append(kwargs)
        return subprocess.CompletedProcess(args, 0, '{"type":"result","result":"OK"}', "")

    directory = tmp_path / "nested" / "requests"
    result = provider_probe.check(provider_probe.Target("claude", "alpha", ""), directory, run)
    assert directory.is_dir()
    assert calls == [{"input": "Reply with exactly OK.", "cwd": directory, "timeout": 30}]
    assert result.ok is True
    assert result.evidence == "Provider request succeeded"
    assert provider_probe.check(provider_probe.Target("claude", "alpha", ""), directory, run).ok is True


@pytest.mark.parametrize(
    "target",
    [
        provider_probe.Target("unknown", "alpha", ""),
        provider_probe.Target("claude", "", ""),
        provider_probe.Target("copilot", "alpha", ""),
    ],
)
def test_missing_native_route_never_launches_a_request(tmp_path, monkeypatch, target):
    monkeypatch.setattr(provider_probe.time, "time_ns", lambda: 987_000_000)
    run = MagicMock()
    result = provider_probe.check(target, tmp_path / "unused", run)
    assert result == provider_probe.Result(False, "Provider account route unavailable", 987)
    run.assert_not_called()
    assert not (tmp_path / "unused").exists()


@pytest.mark.parametrize("error", [OSError("unavailable"), subprocess.TimeoutExpired("request", 30)])
def test_request_exception_retains_failure_evidence_and_completion_time(tmp_path, monkeypatch, error):
    monkeypatch.setattr(provider_probe.time, "time_ns", lambda: 987_654_321)
    run = MagicMock(side_effect=error)
    result = provider_probe.check(provider_probe.Target("claude", "alpha", ""), tmp_path, run)
    assert result == provider_probe.Result(False, f"Provider request failed: {type(error).__name__}", 987)


@pytest.mark.parametrize("returncode", [0, 9])
def test_request_result_records_exact_outcome_and_completion_time(tmp_path, monkeypatch, returncode):
    monkeypatch.setattr(provider_probe.time, "time_ns", lambda: 987_000_000)
    run = MagicMock(return_value=subprocess.CompletedProcess([], returncode, "OK", ""))
    result = provider_probe.check(provider_probe.Target("copilot", "default", ""), tmp_path, run)
    expected = "Provider request succeeded" if returncode == 0 else "Provider request failed: status 9"
    assert result == provider_probe.Result(returncode == 0, expected, 987)


@pytest.mark.parametrize(
    "rows",
    [
        [{"type": "item.completed", "item": {"type": "agent_message", "text": "OK"}}],
        [{"type": "item.completed", "item": {"type": "command_execution", "text": "OK"}}, {"type": "turn.completed"}],
        [{"type": "item.started", "item": {"type": "agent_message", "text": "OK"}}, {"type": "turn.completed"}],
    ],
)
def test_codex_requires_both_a_completed_turn_and_a_completed_model_answer(tmp_path, rows):
    run = MagicMock(return_value=subprocess.CompletedProcess([], 0, "\n".join(map(json.dumps, rows)), ""))
    result = provider_probe.check(provider_probe.Target("codex", "alpha", ""), tmp_path, run)
    assert result.ok is False
    assert result.evidence == "Provider request failed: status 0"


@pytest.mark.parametrize("timeout", [False, True])
def test_transport_pipe_contract_and_timeout_result(tmp_path, monkeypatch, timeout):
    import signal

    child = MagicMock()
    child.pid = 123
    child.returncode = 0
    child.__enter__.return_value = child
    response = ('{"type":"result","result":"OK"}', "")
    child.communicate.side_effect = [subprocess.TimeoutExpired("request", 30), response] if timeout else [response]
    popen = MagicMock(return_value=child)
    kill = MagicMock()
    monkeypatch.setattr(provider_probe.subprocess, "Popen", popen)
    monkeypatch.setattr(provider_probe.os, "killpg", kill)
    result = provider_probe.check(provider_probe.Target("claude", "alpha", ""), tmp_path)
    assert popen.call_args.args[0][:4] == ["agentihooks", "claude", "--route", "alpha"]
    options = popen.call_args.kwargs
    assert options["stdin"] == subprocess.PIPE
    assert options["stdout"] == subprocess.PIPE
    assert options["stderr"] == subprocess.PIPE
    assert options["cwd"] == tmp_path
    assert options["text"] is True
    assert options["start_new_session"] is True
    assert options["env"]["AGENTIHOOKS_CLASSIFIER_CHILD"] == "1"
    assert child.communicate.call_args_list[0].args == ("Reply with exactly OK.",)
    assert child.communicate.call_args_list[0].kwargs == {"timeout": 30}
    assert result.ok is (not timeout)
    assert result.evidence == ("Provider request failed: status 124" if timeout else "Provider request succeeded")
    if timeout:
        kill.assert_called_once_with(123, signal.SIGKILL)
        assert child.communicate.call_args_list[1].args == ()
    else:
        kill.assert_not_called()


def test_account_backoff_keeps_probe_evidence_and_never_rechecks_recovered_accounts():
    target = provider_probe.Target("claude", "alpha", "model")
    states = {provider_probe.key(target): provider_probe.schedule(target, 1000)}
    failure = MagicMock(return_value=provider_probe.Result(False, "unavailable", 62000))
    assert provider_probe.advance(states, 61000, failure) is False
    state = states["claude:alpha"]
    assert state["evidence"] == "unavailable"
    assert state["next_at"] == 182000
    success = MagicMock(return_value=provider_probe.Result(True, "confirmed", 183000))
    assert provider_probe.advance(states, 182000, success) is True
    assert state["evidence"] == "confirmed"
    assert state["succeeded_at"] == 183000
    assert provider_probe.advance(states, 999999, success) is True
    success.assert_called_once_with(target)


def test_account_probes_use_a_bounded_executor(monkeypatch):
    executor = MagicMock()
    executor.return_value.__enter__.return_value.map.side_effect = lambda probe, targets: map(probe, targets)
    monkeypatch.setattr(provider_probe, "ThreadPoolExecutor", executor)
    targets = [provider_probe.Target("claude", str(account), "") for account in range(7)]
    states = {provider_probe.key(target): provider_probe.schedule(target, 1000) for target in targets}
    assert provider_probe.advance(states, 61000, lambda target: provider_probe.Result(True, "confirmed")) is True
    executor.assert_called_once_with(max_workers=3)


def test_codex_valid_answer_survives_an_unrelated_completed_item(tmp_path):
    output = "\n".join(
        [
            '{"type":"item.completed"}',
            '{"type":"item.completed","item":{"type":"agent_message","text":"OK"}}',
            '{"type":"turn.completed"}',
        ]
    )
    run = MagicMock(return_value=subprocess.CompletedProcess([], 0, output, ""))
    assert provider_probe.check(provider_probe.Target("codex", "alpha", ""), tmp_path, run).ok is True
