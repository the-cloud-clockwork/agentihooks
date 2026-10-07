from types import SimpleNamespace

import pytest

import hooks.context.swarm_wait_nudge as nudge
from hooks import hook_manager
from hooks.targets.emitter import flush
from scripts.swarm import idle

pytestmark = pytest.mark.xdist_group("fakeredis")

NAME = "engineer@abc123-0001"
WORKER = {"AGENTIHOOKS_SWARM": "rig", "AGENTIHOOKS_SWARM_TASK": "t7", "AGENTIHOOKS_AGENT_NAME": NAME}
TOLD = 'agentihooks swarm rig wait <minutes> --reason "<what you wait on>"'


@pytest.fixture
def redis(monkeypatch):
    import fakeredis

    fake = fakeredis.FakeRedis(decode_responses=True)
    monkeypatch.setattr("scripts.swarm.store.connect", lambda: SimpleNamespace(redis=fake))
    return fake


def _bash(command, **extra):
    return {"tool_name": "Bash", "tool_input": {"command": command, **extra}}


def _declare(redis, slug="rig", name=NAME):
    idle.declare_wait(redis, slug, name, 10**15, "the after window", 1)


@pytest.mark.parametrize(
    "payload",
    [
        _bash("sleep 1800; python count.py"),
        _bash("cd ~/x && sleep 5m"),
        _bash('until [ "$(gh run view 1 --json status --jq .status)" = completed ]; do sleep 10; done'),
        _bash("while pgrep -f count.py\ndo\n  echo waiting\ndone"),
        _bash("sleep 3600 && python count.py", run_in_background=True),
        _bash('N=1800\n  sleep "$N"'),
        _bash("{ sleep $((60*5)); }"),
        _bash("for i in 1 2; do sleep 60; done"),
        _bash("if pgrep x; then sleep 5; fi"),
        _bash("nohup sleep 900 &"),
        _bash("timeout 600 sleep 5"),
        _bash("gh run watch 37671144724"),
        _bash("gh pr checks 1367 --watch --interval 30"),
        {"tool_name": "Monitor", "tool_input": {"command": "until gh pr checks 1; do sleep 30; done"}},
    ],
)
def test_a_worker_with_no_declared_wait_is_told_to_declare_it(redis, payload):
    told = nudge.nudge(payload, environ=WORKER)
    assert TOLD in told
    assert "You hold task t7" in told
    assert "agentihooks swarm rig wait --on checks <pr url> | reply <inbox item> | task <id>" in told
    assert 'agentihooks swarm rig progress --doing "<what>" --ends-when "<what>" at least every 30 minutes' in told


@pytest.mark.parametrize(
    "payload",
    [
        _bash("ls -la"),
        _bash("git commit -m 'Retry until the server answers'"),
        _bash('git commit -m "retry until green; do not force"'),
        _bash('gh pr comment 1 --body "wait until CI is green; do not merge"'),
        _bash("gh pr checks 1367"),
        _bash("python -c 'import time; time.sleep(1)'"),
        {"tool_name": "Monitor", "tool_input": {"command": "tail -f run.log"}},
        {"tool_name": "Monitor"},
        _bash('agentihooks swarm rig wait 30 --reason "the after window, until 21 32"'),
        {"tool_name": "Bash", "tool_input": {}},
        {"tool_name": "Bash"},
        {"tool_name": "Edit", "tool_input": {"file_path": "a.py"}},
    ],
)
def test_a_call_that_is_not_a_wait_is_not_told(redis, payload):
    assert nudge.nudge(payload, environ=WORKER) == ""


def test_a_worker_with_a_declared_wait_is_not_told(redis):
    _declare(redis)
    assert nudge.nudge(_bash("sleep 600"), environ=WORKER) == ""


def test_a_wait_declared_by_another_agent_or_swarm_does_not_count(redis):
    _declare(redis, name="engineer@abc123-0002")
    _declare(redis, slug="other")
    assert TOLD in nudge.nudge(_bash("sleep 600"), environ=WORKER)


def test_a_worker_whose_declared_wait_ended_is_told_again(redis):
    _declare(redis)
    idle.end_wait(redis, "rig", NAME, 1)
    assert TOLD in nudge.nudge(_bash("sleep 600"), environ=WORKER)


def test_a_ci_worker_is_told(redis):
    environ = {**WORKER, "AGENTIHOOKS_AGENT_NAME": "ci@abc123-0003"}
    assert TOLD in nudge.nudge(_bash("sleep 600"), environ=environ)


@pytest.mark.parametrize(
    "environ",
    [
        {},
        {"AGENTIHOOKS_SWARM": "rig", "AGENTIHOOKS_AGENT_NAME": NAME},
        {"AGENTIHOOKS_SWARM_TASK": "t7", "AGENTIHOOKS_AGENT_NAME": NAME},
        {"AGENTIHOOKS_SWARM": "rig", "AGENTIHOOKS_SWARM_TASK": "t7"},
        {**WORKER, "AGENTIHOOKS_SWARM_TASK": "master", "AGENTIHOOKS_AGENT_NAME": "master@abc123-0002"},
    ],
)
def test_a_session_without_a_swarm_task_is_untouched(redis, environ):
    assert nudge.nudge(_bash("sleep 600"), environ=environ) == ""


@pytest.fixture
def worker(redis, monkeypatch):
    for key, value in WORKER.items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv("AGENTIHOOKS_TARGET", raising=False)
    return redis


def _pre(capsys, payload):
    hook_manager.on_pre_tool_use({"session_id": "s1", "cwd": "/", **payload})
    flush("PreToolUse")
    return capsys.readouterr().out


def test_the_pre_tool_hook_carries_the_nudge_for_a_waiting_worker(worker, capsys):
    assert TOLD in _pre(capsys, _bash("sleep 900"))


def test_the_pre_tool_hook_logs_a_failed_lookup_and_adds_nothing(worker, monkeypatch, capsys):
    logged = []

    def broken(slug, name):
        raise RuntimeError("redis down")

    monkeypatch.setattr(nudge, "declared_wait", broken)
    monkeypatch.setattr(hook_manager, "log", lambda message, data=None: logged.append((message, data)))
    assert TOLD not in _pre(capsys, _bash("sleep 900"))
    assert ("swarm wait nudge failed", {"error": "redis down"}) in logged
