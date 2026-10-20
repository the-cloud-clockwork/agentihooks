from types import SimpleNamespace

import pytest

import hooks.context.swarm_wait_nudge as nudge
from hooks import hook_manager
from hooks.targets.emitter import flush
from scripts.swarm import idle

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
        {"tool_name": "sleep", "tool_input": {"duration_ms": 55000}},
        {"tool_name": "wait", "tool_input": {"cell_id": "measurement"}},
        {"tool_name": "write_stdin", "tool_input": {"session_id": 1, "chars": "", "yield_time_ms": 30000}},
        {"tool_name": "write_stdin", "tool_input": {"session_id": 1, "yield_time_ms": 30000}},
        _bash('text(await tools.exec_command({cmd:"sleep 55"}));'),
        _bash('const r = await tools.exec_command({ cmd: "cd ~/x && sleep 300", workdir: "/w" });'),
        _bash('await tools.exec_command({cmd:"ls"}); await tools.exec_command({cmd:"gh run watch 1"});'),
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
        {"tool_name": "write_stdin", "tool_input": {"session_id": 1, "chars": "y\n", "yield_time_ms": 1000}},
        {"tool_name": "sleepy", "tool_input": {}},
        _bash('text(await tools.exec_command({cmd:"ls -la"}));'),
        _bash('await tools.exec_command({workdir:"/w"});'),
        _bash('text("sleep 55"); await tools.exec_command({cmd:"git status"});'),
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


def _post(capsys, payload):
    hook_manager.on_post_tool_use({"session_id": "s1", "cwd": "/", **payload})
    flush("PostToolUse")
    return capsys.readouterr().out


def test_the_post_tool_hook_carries_the_nudge_where_the_pre_tool_event_cannot(worker, monkeypatch, capsys):
    monkeypatch.setenv("AGENTIHOOKS_TARGET", "codex")
    assert TOLD not in _pre(capsys, _bash("sleep 900"))
    assert TOLD in _post(capsys, _bash("sleep 900"))


def test_the_post_tool_hook_leaves_the_nudge_to_a_pre_tool_event_that_carries_it(worker, capsys):
    assert TOLD in _pre(capsys, _bash("sleep 900"))
    assert TOLD not in _post(capsys, _bash("sleep 900"))


def test_the_post_tool_hook_tells_nothing_once_a_wait_is_declared(worker, monkeypatch, capsys):
    monkeypatch.setenv("AGENTIHOOKS_TARGET", "codex")
    _declare(worker)
    assert TOLD not in _post(capsys, _bash("sleep 900"))


@pytest.mark.parametrize(
    "command",
    [
        "gh run watch 123",
        "gh run watch 123 --interval 60",
        "gh -R owner/repo run watch 123",
        "gh --repo owner/repo run watch 123",
        "gh --hostname github.com run watch 123",
        "gh -Rowner/repo run watch 123",
        "gh pr checks 456 --watch",
        "gh pr checks --watch=true 456",
        "cd repo && gh run watch 123 | tail -n 1",
        "gh pr checks 456 --watch; echo done",
        "bash -lc 'gh run watch 123'",
        "env DEBUG=api /usr/bin/gh run watch 123",
    ],
)
@pytest.mark.parametrize("target", ["claude", "codex", "copilot"])
def test_the_pre_tool_hook_refuses_independent_github_watches(worker, monkeypatch, capsys, command, target):
    monkeypatch.setenv("AGENTIHOOKS_TARGET", target)
    with pytest.raises(hook_manager.BlockAction, match="shared GitHub hourly quota") as blocked:
        _pre(capsys, _bash(command))
    assert "agentihooks swarm rig wait --on checks <pr url>" in str(blocked.value)
    assert str(blocked.value) == (
        "Independent GitHub watches spend the shared GitHub hourly quota. "
        "Use agentihooks swarm rig wait --on checks <pr url>, "
        "or wait --on mutation <run url> for Mutation preflight. The swarm tick resolves these waits centrally."
    )


def test_declaring_a_wait_does_not_allow_an_independent_github_watch(worker, capsys):
    _declare(worker)
    with pytest.raises(hook_manager.BlockAction, match="shared GitHub hourly quota"):
        _pre(capsys, _bash("gh run watch 123"))


@pytest.mark.parametrize(
    "command",
    [
        "gh run view 123 --json status",
        "gh pr checks 456",
        "gh pr checks 456 -w",
        "gh pr view 456 --json state",
        "gh api repos/owner/repo/actions/runs/123",
        "printf '%s' 'gh run watch 123'",
        "echo 'gh pr checks --watch'",
        "echo run watch",
        "gh api repos/owner/repo -f 'body=gh run watch 123'",
        "gh pr checks 456; echo --watch",
    ],
)
def test_the_pre_tool_hook_keeps_single_github_reads_and_literal_watch_text_allowed(worker, capsys, command):
    _pre(capsys, _bash(command))


@pytest.mark.parametrize("name", ["ci@abc123-0001", "engineer@abc123-0001"])
def test_each_swarm_worker_lane_is_refused_github_watches(worker, monkeypatch, capsys, name):
    monkeypatch.setenv("AGENTIHOOKS_AGENT_NAME", name)
    with pytest.raises(hook_manager.BlockAction, match="shared GitHub hourly quota"):
        _pre(capsys, _bash("gh run watch 123"))


@pytest.mark.parametrize("missing", ["AGENTIHOOKS_SWARM", "AGENTIHOOKS_AGENT_NAME"])
def test_sessions_outside_the_swarm_worker_scope_can_watch(worker, monkeypatch, capsys, missing):
    monkeypatch.delenv(missing)
    _pre(capsys, _bash("gh run watch 123"))


def test_the_master_can_watch(worker, monkeypatch, capsys):
    monkeypatch.setenv("AGENTIHOOKS_AGENT_NAME", "master@abc123-0001")
    _pre(capsys, _bash("gh run watch 123"))


def test_a_monitor_cannot_start_an_independent_github_watch(worker):
    with pytest.raises(hook_manager.BlockAction, match="shared GitHub hourly quota"):
        nudge.check_github_watch({"tool_name": "Monitor", "tool_input": {"command": "gh run watch 123"}})


def test_a_monitor_command_alias_cannot_start_an_independent_github_watch(worker):
    with pytest.raises(hook_manager.BlockAction, match="shared GitHub hourly quota"):
        nudge.check_github_watch({"tool_name": "Monitor", "tool_input": {"cmd": "gh run watch 123"}})


@pytest.mark.parametrize("tool_input", [{}, {"command": ""}, {"command": None, "cmd": ""}])
def test_empty_monitor_commands_are_allowed(worker, tool_input):
    assert nudge.check_github_watch({"tool_name": "Monitor", "tool_input": tool_input}) is None
