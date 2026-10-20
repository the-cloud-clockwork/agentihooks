from unittest.mock import patch

import pytest

import hooks.config
from hooks import hook_manager
from hooks.context import quota_policy
from hooks.context.pane_guard import opens_pane
from hooks.context.shell_commands import commands

pytestmark = pytest.mark.unit

OPENERS = [
    "herdr pane split --pane p1 --direction right",
    "herdr pane run p2 'bash run.sh'",
    "herdr tab create --label work",
    "herdr workspace create --cwd /repo",
    "herdr worktree create topic",
    "herdr worktree open topic",
    "herdr --session main pane split",
    "herdr --json pane split",
    "herdr pane --session s split",
    "herdr pane --json --remote-keybindings server split p1",
    "herdr --remote-keybindings server pane split",
    "herdr --machine box tab create",
    "herdr --remote me@host workspace create",
    "agentihooks run-in-terminal --dir /repo -- npm test",
    "agentihooks run-in-terminal --handoff",
    "agentihooks init-agent --dir /repo --name eng",
    "agentihooks init-agent --handoff /doc.md",
    "agentihooks swarm demo master up",
    "agentihooks swarm demo master up --last",
    "agentihooks swarm demo engineer up",
    "/skills/run-in-terminal/scripts/02_run_terminal.sh /repo ls",
    '"$RT"/scripts/02_run_terminal.sh /repo ls',
    "bash ~/.claude/skills/prompt-user-parameter/scripts/open_param_pane.sh in.sh title",
    "bash -e /skills/prompt-user-parameter/scripts/run_param_pane.sh in.sh status p1",
    "cd /skills/scripts && bash open_param_pane.sh in.sh",
    "cd /repo && herdr pane split",
    "env A=1 herdr pane split",
    "bash -c 'herdr tab create'",
]
NOT_OPENERS = [
    "herdr pane read p1",
    "herdr pane list",
    "herdr pane close p1",
    "herdr pane",
    "herdr tab list",
    "herdr tab close t1",
    "herdr workspace list",
    "herdr worktree list",
    "herdr agent read a1",
    "herdr --session main pane list",
    "herdr --json pane read p1",
    "herdr pane --session s list",
    "herdr --session pane split",
    "herdr pane read p1 --lines 'tab create'",
    "herdr pane read p1 split",
    "agentihooks swarm demo start",
    "agentihooks swarm demo tick",
    "agentihooks swarm demo status",
    "agentihooks swarm demo master",
    "agentihooks swarm up",
    "agentihooks ledger up",
    "agentihooks",
    "cat /skills/prompt-user-parameter/scripts/open_param_pane.sh",
    "bash",
    "bash open.sh",
    "bash other.sh run_param_pane.sh",
    "./run.sh",
    "/skills/run-in-terminal/scripts/01_check_interop.sh",
    "echo herdr pane split",
]
HANDOFFS = [
    "agentihooks init-agent --handoff --prompt-file /s/repo/handoff/s1.md",
    "/bin/agentihooks init-agent --handoff --dir /r --prompt-file ~/handoff/s1.md --route b",
    "agentihooks init-agent --handoff --prompt-file /s/repo/handoff/s2.md",
    "agentihooks init-agent --handoff --prompt-file /s/repo/notes/s1.md",
    "agentihooks init-agent --handoff --prompt-file",
    "agentihooks init-agent --handoff",
    "agentihooks init-agent --prompt-file /s/repo/handoff/s1.md",
    "agentihooks run-in-terminal --handoff --prompt-file /s/repo/handoff/s1.md",
]


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch):
    monkeypatch.setattr(hooks.config, "AGENTIHOOKS_HOME", tmp_path)
    for name in ("AGENTIHOOKS_SWARM", "AGENTIHOOKS_AGENT_NAME", "AGENTIHOOKS_SWARM_TASK"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(quota_policy, "evaluate", lambda session: None)
    with patch("hooks._redis.get_redis", return_value=None):
        yield tmp_path


def guard(command: str) -> None:
    hook_manager._check_shell_guards({"tool_name": "Bash", "session_id": "s1", "tool_input": {"command": command}})


def detected(command: str) -> bool:
    return any(opens_pane(tokens) for tokens in commands(command))


@pytest.mark.parametrize("command", OPENERS)
def test_every_former_opener_is_still_detected(command):
    assert detected(command)


@pytest.mark.parametrize("command", OPENERS)
def test_every_former_opener_passes_the_shell_guards_without_any_typed_phrase(command):
    guard(command)


@pytest.mark.parametrize("command", OPENERS)
def test_every_former_opener_passes_the_shell_guards_in_a_swarm_session(monkeypatch, command):
    monkeypatch.setenv("AGENTIHOOKS_SWARM", "demo")
    monkeypatch.setenv("AGENTIHOOKS_AGENT_NAME", "eng-1@demo")
    guard(command)


@pytest.mark.parametrize("command", NOT_OPENERS)
def test_every_other_command_is_not_detected(command):
    assert not detected(command)


def test_no_tokens_is_not_an_opener():
    assert not opens_pane([])


@pytest.mark.parametrize("command", HANDOFFS)
def test_a_handoff_terminal_opens_without_reading_the_quota(monkeypatch, command):
    seen = []
    monkeypatch.setattr(quota_policy, "evaluate", seen.append)
    guard(command)
    assert seen == []
