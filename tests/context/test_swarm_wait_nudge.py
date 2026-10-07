import pytest

import hooks.context.swarm_wait_nudge as nudge
from hooks import hook_manager
from hooks.targets.emitter import flush

WORKER = {
    "AGENTIHOOKS_SWARM": "rig",
    "AGENTIHOOKS_SWARM_TASK": "t7",
    "AGENTIHOOKS_AGENT_NAME": "engineer@abc123-0001",
}
NOW = 1_000_000


def _bash(command, **extra):
    return "Bash", {"command": command, **extra}


def _told(tool, environ=WORKER, held=None):
    return nudge.nudge(*tool, environ=environ, held=lambda slug, name: held, now_ms=NOW)


@pytest.mark.parametrize(
    "tool",
    [
        _bash("sleep 1800; python count.py"),
        _bash("cd ~/x && sleep 5m"),
        _bash('until [ "$(gh run view 1 --json status --jq .status)" = completed ]; do sleep 10; done'),
        _bash("while pgrep -f count.py; do sleep 30; done"),
        _bash("sleep 3600 && python count.py", run_in_background=True),
        ("Monitor", {"command": "tail -f run.log"}),
    ],
)
def test_a_worker_with_no_declared_wait_is_told_to_declare_it(tool):
    told = _told(tool)
    assert 'agentihooks swarm rig wait <minutes> --reason "<what you wait on>"' in told
    assert "t7" in told


@pytest.mark.parametrize(
    "tool",
    [
        _bash("ls -la"),
        _bash("git commit -m 'Retry until the server answers'"),
        _bash('agentihooks swarm rig wait 30 --reason "the after window, until 21 32"'),
        ("Edit", {"file_path": "a.py"}),
    ],
)
def test_a_call_that_is_not_a_wait_is_not_told(tool):
    assert _told(tool) == ""


def test_a_worker_with_a_declared_wait_is_not_told():
    assert _told(_bash("sleep 600"), held={"until": NOW + 60_000, "reason": "window"}) == ""


def test_a_worker_whose_declared_wait_ended_is_told_again():
    assert "swarm rig wait" in _told(_bash("sleep 600"), held={"until": NOW - 1, "reason": "window"})


@pytest.mark.parametrize(
    "environ",
    [
        {},
        {"AGENTIHOOKS_SWARM": "rig", "AGENTIHOOKS_AGENT_NAME": "engineer@abc123-0001"},
        {**WORKER, "AGENTIHOOKS_SWARM_TASK": "master", "AGENTIHOOKS_AGENT_NAME": "master@abc123-0002"},
    ],
)
def test_a_session_without_a_swarm_task_is_untouched(environ):
    assert _told(_bash("sleep 600"), environ=environ) == ""


def test_the_pre_tool_hook_carries_the_nudge_for_a_waiting_worker(monkeypatch, capsys):
    for key, value in WORKER.items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv("AGENTIHOOKS_TARGET", raising=False)
    monkeypatch.setattr(nudge, "declared_wait", lambda slug, name: None)
    hook_manager.on_pre_tool_use(
        {"session_id": "s1", "tool_name": "Bash", "tool_input": {"command": "sleep 900"}, "cwd": "/"}
    )
    flush("PreToolUse")
    assert "agentihooks swarm rig wait <minutes>" in capsys.readouterr().out
