import pytest

import hooks.context.context_recycle as recycle
from scripts.codex_context import CodexContext

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _isolated(monkeypatch, tmp_path):
    monkeypatch.setattr(recycle, "AGENTIHOOKS_HOME", tmp_path)
    monkeypatch.setattr(recycle, "COMPACT_LIMIT", 600)
    monkeypatch.setattr(recycle, "used_tokens", lambda session_id: None)
    monkeypatch.setattr(recycle, "codex_context", lambda session_id: None)


def _swarm_env(name="sw-eng-1"):
    return {"AGENTIHOOKS_AGENT_NAME": name}


def test_default_limit_is_600_thousand():
    import hooks.config as config

    assert config.COMPACT_LIMIT == 600


def test_claude_agent_at_the_limit_gets_the_handoff_directive(monkeypatch):
    monkeypatch.setattr(recycle, "used_tokens", lambda session_id: 600_000)
    text = recycle.directive("s1", _swarm_env("my-swarm-ci-2"))
    assert "agentihooks swarm my-swarm handoff" in text and "handoff document" in text
    assert "--recap" in text and "agentihooks swarm my-swarm learned" in text


def test_codex_agent_over_the_limit_gets_the_directive(monkeypatch):
    monkeypatch.setattr(recycle, "codex_context", lambda session_id: CodexContext(used=700_000, window=872_000))
    assert recycle.directive("s1", _swarm_env()) is not None


def test_below_the_limit_nothing(monkeypatch):
    monkeypatch.setattr(recycle, "used_tokens", lambda session_id: 599_999)
    assert recycle.directive("s1", _swarm_env()) is None


def test_unknown_usage_nothing():
    assert recycle.directive("s1", _swarm_env()) is None


def test_non_swarm_session_nothing(monkeypatch):
    monkeypatch.setattr(recycle, "used_tokens", lambda session_id: 900_000)
    assert recycle.directive("s1", {}) is None
    assert recycle.directive("s1", {"AGENTIHOOKS_AGENT_NAME": "my terminal"}) is None


def test_directive_is_given_once_per_session(monkeypatch):
    monkeypatch.setattr(recycle, "used_tokens", lambda session_id: 900_000)
    assert recycle.directive("s1", _swarm_env()) is not None
    assert recycle.directive("s1", _swarm_env()) is None
    assert recycle.directive("s2", _swarm_env()) is not None


def test_the_swarm_master_gets_the_directive_too(monkeypatch):
    monkeypatch.setattr(recycle, "used_tokens", lambda session_id: 600_000)
    assert "agentihooks swarm my-swarm handoff" in recycle.directive("s1", _swarm_env("my-swarm-master-3"))


def _over(monkeypatch, tokens=700_000):
    monkeypatch.setattr(recycle, "used_tokens", lambda session_id: tokens)


def _gate(tool_name, tool_input, env=None):
    return recycle.gate(tool_name, tool_input, "s1", _swarm_env("my-swarm-eng-4") if env is None else env)


def test_recycle_check_returns_the_slug_only_at_or_over_the_limit(monkeypatch):
    _over(monkeypatch, 600_000)
    assert recycle.over_limit("s1", _swarm_env("my-swarm-master-1")) == "my-swarm"
    _over(monkeypatch, 599_999)
    assert recycle.over_limit("s1", _swarm_env("my-swarm-master-1")) is None


@pytest.mark.parametrize(
    "tool_name, tool_input",
    [
        ("Edit", {"file_path": "/home/u/dev/worktrees/repo/x/hooks/a.py", "old_string": "a", "new_string": "b"}),
        ("Write", {"file_path": "/home/u/dev/repo/notes.md", "content": "x"}),
        ("Bash", {"command": "git commit -m 'more work'"}),
        ("Bash", {"command": "agentihooks swarm my-swarm handoff ~/scratchpad/h.md && git commit -m x"}),
        ("Bash", {"command": "agentihooks ledger --slug my-swarm --as a say hi; git push"}),
        ("Bash", {"command": "agentihooks swarm my-swarm handoff $(git commit -m x)"}),
        ("Bash", {"command": "agentihooks swarm my-swarm done --pr x"}),
        ("Bash", {"command": "agentihooks swarm my-swarm handoff ~/scratchpad/h.md --pr x"}),
        ("Bash", {"command": "agentihooks swarm my-swarm handoff ~/scratchpad/h.md --recap"}),
        ("Agent", {"prompt": "keep going"}),
    ],
)
def test_over_the_limit_work_is_denied_with_the_handoff_command(monkeypatch, tool_name, tool_input):
    _over(monkeypatch)
    for _ in range(2):
        reason = _gate(tool_name, tool_input)
        assert reason and "agentihooks swarm my-swarm handoff" in reason


@pytest.mark.parametrize(
    "tool_name, tool_input",
    [
        ("Read", {"file_path": "/home/u/dev/repo/hooks/a.py"}),
        ("Grep", {"pattern": "x"}),
        ("Glob", {"pattern": "*.py"}),
        ("Write", {"file_path": "~/scratchpad/agentihooks/t28/handoff.md", "content": "state"}),
        ("Edit", {"file_path": "{home}/scratchpad/agentihooks/t28/handoff.md", "old_string": "a", "new_string": "b"}),
        ("Bash", {"command": "agentihooks swarm my-swarm handoff ~/scratchpad/agentihooks/t28/handoff.md"}),
        ("Bash", {"command": "agentihooks swarm my-swarm handoff ~/scratchpad/h.md --recap ~/scratchpad/r.md"}),
        ("Bash", {"command": "agentihooks swarm my-swarm learned 'run the whole suite, not -m unit'"}),
        ("Bash", {"command": 'agentihooks ledger --slug my-swarm --as my-swarm-eng-4 comment phases/p1 "handed off"'}),
        ("Bash", {"command": "agentihooks ledger --slug my-swarm --as my-swarm-eng-4 say 'handing off now'"}),
        ("Bash", {"command": "agentihooks ledger --slug my-swarm --as my-swarm-eng-4 leave"}),
        ("Bash", {"command": "agentihooks ledger --slug my-swarm --as my-swarm-eng-4 ack"}),
    ],
)
def test_over_the_limit_the_handoff_steps_are_allowed(monkeypatch, tool_name, tool_input):
    _over(monkeypatch)
    if "file_path" in tool_input:
        tool_input = {**tool_input, "file_path": tool_input["file_path"].format(home=str(recycle.Path.home()))}
    assert _gate(tool_name, tool_input) is None


def test_a_write_that_escapes_the_scratchpad_is_denied(monkeypatch):
    _over(monkeypatch)
    assert _gate("Write", {"file_path": "~/scratchpad/../dev/repo/a.py", "content": "x"})


def test_under_the_limit_everything_is_allowed(monkeypatch):
    _over(monkeypatch, 599_999)
    assert _gate("Bash", {"command": "git commit -m x"}) is None
    assert _gate("Edit", {"file_path": "/repo/a.py"}) is None


def test_a_session_that_is_not_a_swarm_agent_is_never_blocked(monkeypatch):
    _over(monkeypatch, 900_000)
    assert _gate("Bash", {"command": "git commit -m x"}, env={}) is None
    assert _gate("Edit", {"file_path": "/repo/a.py"}, env={"AGENTIHOOKS_AGENT_NAME": "my terminal"}) is None


def test_the_pre_tool_hook_denies_work_over_the_limit(monkeypatch):
    from hooks import hook_manager

    _over(monkeypatch)
    monkeypatch.setenv("AGENTIHOOKS_AGENT_NAME", "my-swarm-eng-4")
    with pytest.raises(hook_manager.BlockAction, match="agentihooks swarm my-swarm handoff"):
        hook_manager.on_pre_tool_use(
            {"session_id": "s1", "tool_name": "Bash", "tool_input": {"command": "git commit -m x"}, "cwd": "/"}
        )


def test_a_codex_patch_that_also_touches_code_is_denied(monkeypatch):
    _over(monkeypatch)
    home = recycle.Path.home()
    patch = (
        "*** Begin Patch\n"
        f"*** Add File: {home}/scratchpad/agentihooks/t28/handoff.md\n+state\n"
        "*** Update File: /repo/hooks/a.py\n@@\n-a\n+b\n"
        "*** End Patch\n"
    )
    tool_input = {"file_path": f"{home}/scratchpad/agentihooks/t28/handoff.md", "content": patch}
    assert _gate("Edit", tool_input)
    tool_input["content"] = patch.split("*** Update File")[0] + "*** End Patch\n"
    assert _gate("Edit", tool_input) is None


@pytest.mark.parametrize(
    "command",
    [
        "cat hooks/a.py",
        "head -n 40 hooks/a.py",
        "git status --short",
        "git log --oneline -5",
        "grep -rn handoff hooks",
        "ls",
    ],
)
def test_over_the_limit_a_codex_agent_can_still_read_through_its_shell(monkeypatch, command):
    _over(monkeypatch)
    assert _gate("Bash", {"command": command}) is None


@pytest.mark.parametrize("command", ["cat a.py > b.py", "cat a.py | tee b.py", "git push", "git diff --output=x"])
def test_over_the_limit_a_shell_read_that_writes_is_denied(monkeypatch, command):
    _over(monkeypatch)
    assert _gate("Bash", {"command": command})
