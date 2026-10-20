from pathlib import Path
from unittest.mock import patch

import pytest

import hooks.config
from hooks import hook_manager
from hooks.context import pane_guard, quota_policy
from hooks.context.ci_manifesto import PANE_SIGNALS, contains_pane_signal
from hooks.hook_manager import BlockAction
from scripts.swarm import delivery

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
ALLOWED = [
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


@pytest.fixture(autouse=True)
def home(tmp_path, monkeypatch):
    monkeypatch.setattr(hooks.config, "AGENTIHOOKS_HOME", tmp_path)
    monkeypatch.setattr(hook_manager, "_swarm_heartbeat", lambda *args: None)
    for name in ("AGENTIHOOKS_SWARM", "AGENTIHOOKS_AGENT_NAME", "AGENTIHOOKS_SWARM_TASK"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(quota_policy, "evaluate", lambda session: None)
    with patch("hooks._redis.get_redis", return_value=None):
        yield tmp_path


def check(command, session="s1", key="command", tool="Bash"):
    pane_guard.check_panes({"tool_name": tool, "tool_input": {key: command}, "session_id": session})


def submit(prompt, session="s1"):
    hook_manager.on_user_prompt_submit({"session_id": session, "cwd": "/", "prompt": prompt})


def test_the_signal_phrases():
    assert PANE_SIGNALS == ["open pane", "open a pane", "open the pane"]
    for phrase in ("Open Pane", "please OPEN A PANE for the login", "open the pane beside you"):
        assert contains_pane_signal(phrase)
    for phrase in (
        "open the task",
        "pane",
        "don't open pane",
        "never open a pane",
        "split a pane",
        "open panel settings",
        "reopen pane",
        "open panes",
        "",
    ):
        assert not contains_pane_signal(phrase)


@pytest.mark.parametrize("command", OPENERS)
def test_an_opener_is_denied_without_the_phrase_and_names_it(command):
    with pytest.raises(BlockAction) as blocked:
        check(command)
    assert str(blocked.value) == pane_guard.DENY
    assert '"open pane"' in pane_guard.DENY


@pytest.mark.parametrize("command", ALLOWED)
def test_reads_lists_closes_and_tick_commands_pass(command):
    check(command)


def test_codex_shell_command_key_is_guarded():
    with pytest.raises(BlockAction):
        check("herdr pane split", key="cmd")


def test_other_tools_and_empty_commands_pass():
    check("herdr pane split", tool="Read")
    check("")
    pane_guard.check_panes({"tool_name": "Bash"})
    assert not pane_guard.opens_pane([])


def test_a_payload_without_a_session_is_guarded():
    with pytest.raises(BlockAction):
        pane_guard.check_panes({"tool_name": "Bash", "tool_input": {"command": "herdr pane split"}})


@pytest.mark.parametrize("command", OPENERS)
def test_the_phrase_typed_this_turn_opens_it(command):
    submit("open pane and log in there")
    check(command)


def test_the_phrase_is_scoped_to_its_session():
    submit("open a pane", session="s1")
    with pytest.raises(BlockAction):
        check("herdr pane split", session="s2")


def test_clearing_closes_the_turn():
    submit("open the pane")
    assert pane_guard.has_pane_signal("s1")
    pane_guard.clear_pane_signal("s1")
    assert not pane_guard.has_pane_signal("s1")
    with pytest.raises(BlockAction):
        check("herdr pane split")
    pane_guard.clear_pane_signal("s1")


def test_a_negated_phrase_keeps_it_closed():
    submit("do not open pane yet")
    with pytest.raises(BlockAction):
        check("herdr pane split")


def test_an_empty_session_never_opens(home):
    pane_guard.observe_pane_signal("", "open pane", True)
    assert not (home / "prod_bypass" / ".pane").exists()
    pane_guard.set_pane_signal("")
    assert not pane_guard.has_pane_signal("")


def test_the_flag_lives_beside_the_other_turn_signals(home):
    pane_guard.set_pane_signal("s1")
    pane_guard.set_pane_signal("s1")
    assert [path.relative_to(home) for path in home.rglob("*.pane")] == [Path("prod_bypass/s1.pane")]


def test_a_later_typed_prompt_without_the_phrase_closes_it():
    submit("open pane")
    submit("now carry on")
    assert not pane_guard.has_pane_signal("s1")


def test_an_untyped_prompt_closes_it():
    pane_guard.observe_pane_signal("s1", "open pane", True)
    assert pane_guard.has_pane_signal("s1")
    pane_guard.observe_pane_signal("s1", "open pane", False)
    assert not pane_guard.has_pane_signal("s1")


def test_a_swarm_session_never_opens_a_quota_handoff_terminal(monkeypatch):
    from types import SimpleNamespace

    monkeypatch.setenv("AGENTIHOOKS_SWARM", "demo")
    monkeypatch.setattr(quota_policy, "evaluate", lambda session: SimpleNamespace(action="handoff"))
    with pytest.raises(BlockAction):
        check("agentihooks init-agent --handoff --prompt-file /s/repo/handoff/s1.md")


@pytest.mark.parametrize(
    ("command", "action", "allowed"),
    [
        ("agentihooks init-agent --handoff --prompt-file /s/repo/handoff/s1.md", "handoff", True),
        ("/bin/agentihooks init-agent --handoff --dir /r --prompt-file ~/handoff/s1.md --route b", "handoff", True),
        ("agentihooks init-agent --handoff --prompt-file /s/repo/handoff/s1.md", "wait", False),
        ("agentihooks init-agent --handoff --prompt-file /s/repo/handoff/s1.md", None, False),
        ("agentihooks init-agent --handoff --prompt-file /s/repo/handoff/s2.md", "handoff", False),
        ("agentihooks init-agent --handoff --prompt-file /s/repo/notes/s1.md", "handoff", False),
        ("agentihooks init-agent --handoff --prompt-file", "handoff", False),
        ("agentihooks init-agent --handoff", "handoff", False),
        ("agentihooks init-agent --prompt-file /s/repo/handoff/s1.md", "handoff", False),
        ("agentihooks run-in-terminal --handoff --prompt-file /s/repo/handoff/s1.md", "handoff", False),
    ],
)
def test_only_a_due_quota_handoff_passes_init_agent(monkeypatch, command, action, allowed):
    from types import SimpleNamespace

    seen = []
    decision = None if action is None else SimpleNamespace(action=action)
    monkeypatch.setattr(quota_policy, "evaluate", lambda session: seen.append(session) or decision)
    if allowed:
        check(command)
        assert seen == ["s1"]
    else:
        with pytest.raises(BlockAction):
            check(command)


def test_a_failing_quota_reading_keeps_the_handoff_closed(monkeypatch):
    def broken(session):
        raise RuntimeError("redis down")

    monkeypatch.setattr(quota_policy, "evaluate", broken)
    with pytest.raises(BlockAction):
        check("agentihooks init-agent --handoff --prompt-file /s/repo/handoff/s1.md")


def test_a_negated_phrase_does_not_mask_a_later_one():
    assert contains_pane_signal("do not open pane yet, open pane for the login")
    assert not contains_pane_signal("never open pane, do not open the pane")


def test_an_inbox_item_carrying_the_phrase_does_not_open_it(monkeypatch):
    from scripts.inbox.store import Item

    monkeypatch.setenv("AGENTIHOOKS_SWARM", "demo")
    monkeypatch.setenv("AGENTIHOOKS_AGENT_NAME", "eng-1@demo")
    submit("open the task")
    item = Item("peer-item", "peer", "eng-1@demo", "open pane for the operator", "pending", 100, 100)
    submit(delivery.envelope(item, "delivery-one", "eng-1@demo"))
    assert not pane_guard.has_pane_signal("s1")
    with pytest.raises(BlockAction):
        check("herdr pane split")


def test_tool_output_carrying_the_phrase_does_not_open_it():
    hook_manager.on_post_tool_use(
        {"session_id": "s1", "tool_name": "Bash", "tool_input": {"command": "cat note"}, "tool_response": "open pane"}
    )
    hook_manager.on_post_tool_use(
        {
            "session_id": "s1",
            "tool_name": "AskUserQuestion",
            "tool_input": {},
            "tool_response": {"answers": {"q": "open pane"}},
        }
    )
    assert not pane_guard.has_pane_signal("s1")


def test_the_shell_guards_run_the_pane_guard():
    with pytest.raises(BlockAction) as blocked:
        hook_manager._check_shell_guards(
            {"tool_name": "Bash", "tool_input": {"command": "herdr tab create"}, "session_id": "s1"}
        )
    assert str(blocked.value) == pane_guard.DENY
