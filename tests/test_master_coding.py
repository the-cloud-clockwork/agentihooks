import json
import runpy
from pathlib import Path
from unittest.mock import Mock

import pytest

from hooks import config, hook_manager
from hooks.context import master_coding, operator_mode

ROOT = Path(__file__).resolve().parents[1]
GUARD = runpy.run_path(str(ROOT / "profiles" / "package" / "roles" / "_guards" / "no_code_edits.py"))
SID = "master-session"


@pytest.fixture(autouse=True)
def scheduler_storage(monkeypatch):
    import fakeredis

    redis = fakeredis.FakeRedis(decode_responses=True)
    monkeypatch.setattr("scripts.swarm.store.redis_client", lambda: redis)
    monkeypatch.setattr("scripts.scheduler.service.ensure_running", lambda: True)


@pytest.fixture
def master(tmp_path, monkeypatch):
    for key, value in {"AGENTIHOOKS_SWARM": "rig", "AGENTIHOOKS_SWARM_TASK": "master"}.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("AGENTIHOOKS_HOME", str(tmp_path / "ah"))
    return tmp_path


@pytest.fixture
def unseated(tmp_path, monkeypatch):
    monkeypatch.delenv("AGENTIHOOKS_SWARM", raising=False)
    monkeypatch.delenv("AGENTIHOOKS_SWARM_TASK", raising=False)
    monkeypatch.setenv("AGENTIHOOKS_HOME", str(tmp_path / "ah"))
    monkeypatch.setenv("LEDGER_DIR", str(tmp_path / "ledgers"))
    return tmp_path


def test_a_seat_record_opens_the_window_without_the_swarm_env(unseated):
    assert master_coding.observe(SID, "allow-master-coding", typed=True) == ""
    master_coding.record_seat(SID, "rig", "master@rig-1")
    assert master_coding.observe(SID, "allow-master-coding", typed=True, now=1000) == "open"
    assert master_coding.is_open(SID)
    assert master_coding.observe(SID, "disallow-master-coding", typed=True) == "closed"
    assert not master_coding.is_open(SID)


def test_a_hand_launched_session_without_env_opens_edits_once_seated(unseated):
    tree = unseated / "dev" / "worktrees" / "agentihooks" / "master-1"
    master_coding.record_seat(SID, "rig", "master@rig-1")
    assert "cannot edit outside" in guard(unseated, tree / "hooks" / "x.py")
    master_coding.observe(SID, "allow-master-coding", typed=True)
    assert guard(unseated, tree / "hooks" / "x.py") == ""
    assert "stay closed" in guard(unseated, tree / ".env")
    assert "stay closed" in guard(unseated, tree / ".claude" / "conditions" / "pre-bash-x.py")
    assert "cannot edit outside" in guard(unseated, unseated / "dev" / "tcc-ecosystem" / "x.py")


def test_an_unseated_session_stays_refused_even_with_an_open_window(unseated):
    tree = unseated / "dev" / "worktrees" / "agentihooks" / "master-1"
    record = master_coding.path(SID)
    record.parent.mkdir(parents=True)
    record.write_text(json.dumps({"at": master_coding.time.time()}))
    assert "cannot edit outside" in guard(unseated, tree / "hooks" / "x.py")
    assert not master_coding.is_open(SID)


def test_only_the_typed_phrase_in_the_master_pane_opens_the_window(master):
    assert master_coding.observe(SID, "allow-master-coding", typed=False) == ""
    assert not master_coding.is_open(SID)
    assert master_coding.observe(SID, "please allow-master-coding now", typed=True, now=1000) == "open"
    assert master_coding.is_open(SID)
    assert master_coding.observe(SID, None, typed=True) == ""
    assert master_coding.observe(None, "allow-master-coding", typed=True) == ""


def test_disallow_closes_the_window_and_never_opens_it(master):
    master_coding.observe(SID, "allow-master-coding", typed=True)
    assert master_coding.observe(SID, "disallow-master-coding", typed=True) == "closed"
    assert not master_coding.is_open(SID)
    assert master_coding.observe(SID, "disallow-master-coding", typed=True) == "closed"
    assert not master_coding.is_open(SID)


def test_other_seats_never_open_a_window(master, monkeypatch):
    monkeypatch.setenv("AGENTIHOOKS_SWARM_TASK", "t12")
    assert master_coding.observe(SID, "allow-master-coding", typed=True) == ""
    assert not master_coding.path(SID).exists()
    assert not master_coding.is_open(SID)


def test_the_window_record_lives_under_the_agentihooks_home_by_a_safe_session_name(master, monkeypatch):
    assert master_coding.path("Ab/c d") == master / "ah" / "master_coding" / "Ab_c_d.json"
    assert master_coding.seat_path("Ab/c d.@-") == master / "ah" / "master_seats" / "Ab_c_d.@-.json"
    monkeypatch.delenv("AGENTIHOOKS_HOME")
    monkeypatch.setattr(Path, "home", lambda: master)
    assert master_coding.path("s") == master / ".agentihooks" / "master_coding" / "s.json"
    assert master_coding.seat_path("s") == master / ".agentihooks" / "master_seats" / "s.json"


def guard(home, file_path):
    payload = {"tool_name": "Edit", "tool_input": {"file_path": str(file_path)}, "session_id": SID, "cwd": "/"}
    return GUARD["deny_reason"](payload, "master", home)


def test_the_operator_can_renew_an_already_open_window(master):
    assert master_coding.observe(SID, "allow-master-coding", typed=True, now=1000) == "open"
    assert master_coding.observe(SID, "allow-master-coding", typed=True, now=1001) == "open"
    assert master_coding.is_open(SID)


def test_a_taken_master_keeps_protection_checks_without_env(unseated, monkeypatch):
    master_coding.record_seat(SID, "rig", "master@rig-1")
    payload = {"session_id": SID, "tool_input": {"command": "gh ruleset list"}}
    assert master_coding.protection_refusal(payload) == master_coding.PROTECTION_REFUSAL
    payload["session_id"] = "unseated"
    assert master_coding.protection_refusal(payload) == ""


@pytest.mark.parametrize(
    ("tool", "key"),
    [("Edit", "file_path"), ("Write", "file_path"), ("mcp__serena__replace_symbol_body", "relative_path")],
)
@pytest.mark.parametrize("seat", ["master", "master-2"])
def test_a_master_subagent_edits_only_inside_a_worktree_without_a_window(master, monkeypatch, tool, key, seat):
    monkeypatch.setenv("AGENTIHOOKS_SWARM_TASK", seat)
    tree = master / "dev" / "worktrees" / "agentihooks" / "child"
    payload = {
        "tool_name": tool,
        "tool_input": {key: str(tree / "hooks" / "x.py")},
        "session_id": SID,
        "agent_id": "child-a",
        "cwd": "/",
    }
    assert GUARD["deny_reason"](payload, "master", master) == ""
    for target in [
        master / "dev" / "tcc-ecosystem" / "agentihooks" / "x.py",
        tree / ".env",
        tree / ".npmrc",
        tree / ".claude" / "conditions" / "pre-bash-x.py",
    ]:
        payload["tool_input"] = {key: str(target)}
        assert GUARD["deny_reason"](payload, "master", master)


def test_a_hand_launched_masters_subagent_uses_the_recorded_seat(unseated):
    tree = unseated / "dev" / "worktrees" / "agentihooks" / "child"
    payload = {
        "tool_name": "Edit",
        "tool_input": {"file_path": str(tree / "x.py")},
        "session_id": SID,
        "agent_id": "child-a",
        "cwd": "/",
    }
    assert GUARD["deny_reason"](payload, "master", unseated)
    master_coding.record_seat(SID, "rig", "master@rig-1")
    assert GUARD["deny_reason"](payload, "master", unseated) == ""
    assert GUARD["deny_reason"](payload, "planner", unseated)
    payload.pop("agent_id")
    assert GUARD["deny_reason"](payload, "master", unseated)


def test_a_master_subagent_cannot_follow_a_worktree_symlink_outside(master):
    tree = master / "dev" / "worktrees" / "agentihooks" / "child"
    tree.mkdir(parents=True)
    (tree / "outside").symlink_to(master / "dev" / "tcc-ecosystem")
    payload = {
        "tool_name": "Edit",
        "tool_input": {"file_path": str(tree / "outside" / "x.py")},
        "session_id": SID,
        "agent_id": "child-a",
        "cwd": "/",
    }
    assert GUARD["deny_reason"](payload, "master", master)


def test_the_window_opens_worktree_edits_but_keeps_conditions_and_credentials_closed(master):
    tree = master / "dev" / "worktrees" / "agentihooks" / "master-1"
    assert "cannot edit outside" in guard(master, tree / "hooks" / "x.py")
    master_coding.observe(SID, "allow-master-coding", typed=True)
    assert guard(master, tree / "hooks" / "x.py") == ""
    assert "stay closed" in guard(master, tree / ".claude" / "conditions" / "pre-bash-x.py")
    assert "stay closed" in guard(master, tree / ".env")
    assert guard(master, tree / ".env.example") == ""
    assert "cannot edit outside" in guard(master, master / "dev" / "tcc-ecosystem" / "agentihooks" / "x.py")


def test_the_window_opens_serena_edits_by_project_relative_path(master):
    payload = {
        "tool_name": "mcp__serena__replace_content",
        "tool_input": {"relative_path": "scripts/swarm/master_launch.py"},
        "session_id": SID,
        "cwd": str(master / "dev" / "tcc-ecosystem" / "agentihooks"),
    }
    assert "cannot edit outside" in GUARD["deny_reason"](payload, "master", master)
    master_coding.observe(SID, "allow-master-coding", typed=True)
    assert GUARD["deny_reason"](payload, "master", master) == ""
    payload["tool_input"] = {"relative_path": ".claude/conditions/pre-bash-x.py"}
    assert "stay closed" in GUARD["deny_reason"](payload, "master", master)
    payload["tool_input"] = {"relative_path": ""}
    assert "cannot edit outside" in GUARD["deny_reason"](payload, "master", master)
    payload["tool_name"] = "mcp__serena__rename_symbol"
    payload["tool_input"] = {"relative_path": "scripts/swarm/master_launch.py"}
    assert "cannot edit outside" in GUARD["deny_reason"](payload, "master", master)


def test_an_old_window_record_opens_edits_until_explicitly_closed(master):
    record = master_coding.path(SID)
    record.parent.mkdir(parents=True)
    record.write_text(json.dumps({"at": 0}))
    target = master / "dev" / "worktrees" / "a" / "b" / "x.py"
    assert guard(master, target) == ""
    master_coding.close(SID)
    assert "cannot edit outside" in guard(master, target)


def test_the_typed_prompt_opens_the_window_and_tells_the_master(master, monkeypatch):
    shown = Mock()
    monkeypatch.setattr("hooks.common.inject_context", shown)
    hook_manager._master_coding({"session_id": SID, "prompt": "allow-master-coding"}, True)
    shown.assert_called_once_with(
        "Master hotfix window open until deny-master-coding or a session restart: the master may create a worktree with wt.sh new, edit "
        "inside it, commit, push its branch and merge its pull request into dev on green. Main, rulesets, branch "
        "protection, condition folders, credential files and secrets stay closed. deny-master-coding ends it.",
        also_log=False,
        skip_compression=True,
        kind="alert",
    )
    assert master_coding.is_open(SID)


def test_a_failing_window_update_is_logged_with_its_error(master, monkeypatch):
    logged = Mock()
    monkeypatch.setattr(hook_manager, "log", logged)
    monkeypatch.setattr(master_coding, "observe", Mock(side_effect=RuntimeError("boom")))
    hook_manager._master_coding({"session_id": SID, "prompt": "allow-master-coding"}, True)
    logged.assert_called_once_with("master coding window failed", {"error": "boom"})


def test_every_typed_prompt_reaches_the_window(monkeypatch):
    monkeypatch.setattr(config, "AMYGDALA_ENABLED", False)
    monkeypatch.setattr(config, "SECRETS_MODE", "off")
    for flag in (
        "QUOTA_POLICY_ENABLED",
        "QUOTA_USAGE_INJECTION_ENABLED",
        "CI_MANIFESTO_ENABLED",
        "VOICE_ENABLED",
        "CONTROLS_BYPASS_ENABLED",
        "BROADCAST_ENABLED",
    ):
        monkeypatch.setattr(config, flag, False)
    for name in (
        "_confirm_inbox",
        "_swarm_heartbeat",
        "_operator_mode",
        "_request_trace_flush",
        "_inject_refocus",
        "_inject_ledger_decision",
    ):
        monkeypatch.setattr(hook_manager, name, Mock())
    monkeypatch.setattr(hook_manager, "_operator_words", Mock(return_value=True))
    window = Mock()
    monkeypatch.setattr(hook_manager, "_master_coding", window)
    monkeypatch.setattr("hooks.context.rules_refresh.maybe_inject", Mock())
    monkeypatch.setattr("hooks.context.enforcement.get_user_prompt_enforcements", Mock(return_value=""))
    payload = {"session_id": "session", "prompt": "", "cwd": ""}
    hook_manager.on_user_prompt_submit(payload)
    window.assert_called_once_with(payload, True)


def test_any_seat_asks_the_operator_only_after_he_typed_operator_on(master, monkeypatch):
    heard = Mock(return_value=True)
    monkeypatch.setattr(operator_mode, "_heard", heard)
    assert master_coding.asks_operator("AskUserQuestion", SID)
    heard.assert_called_with(SID)
    assert master_coding.asks_operator("ExitPlanMode", SID)
    assert not master_coding.asks_operator("Bash", SID)
    monkeypatch.setenv("AGENTIHOOKS_SWARM_TASK", "t12")
    assert master_coding.asks_operator("AskUserQuestion", SID)
    heard.return_value = False
    assert not master_coding.asks_operator("AskUserQuestion", SID)
    assert not master_coding.asks_operator("ExitPlanMode", SID)


def _permission(capsys, tool_name, session_id):
    hook_manager.on_permission_request({"tool_name": tool_name, "session_id": session_id})
    out = capsys.readouterr().out
    return json.loads(out)["hookSpecificOutput"]["decision"] if out else None


PROMPT_REFUSAL = (
    "Denied {tool}: the operator is not present in this pane, and only his typed operator on opens this prompt. "
    "Keep working without it."
)


@pytest.mark.parametrize("task", ["master", "t12"])
def test_the_question_and_plan_prompts_reach_the_operator_only_after_operator_on(master, monkeypatch, capsys, task):
    monkeypatch.setenv("AGENTIHOOKS_SWARM_TASK", task)
    for tool in ("AskUserQuestion", "ExitPlanMode"):
        assert _permission(capsys, tool, SID) == {"behavior": "deny", "message": PROMPT_REFUSAL.format(tool=tool)}
    operator_mode.observe(SID, "rebase it", True, now=100)
    assert _permission(capsys, "ExitPlanMode", SID)["behavior"] == "deny"
    operator_mode.observe(SID, "operator on", True, now=101)
    assert _permission(capsys, "AskUserQuestion", SID) is None
    assert _permission(capsys, "ExitPlanMode", SID) is None
    assert "swarm sessions and subagents cannot wait for permission" in _permission(capsys, "Bash", SID)["message"]


def test_a_standalone_session_gets_every_permission_prompt_without_operator_on(unseated, capsys):
    for prompt in ("plan it", "operator off"):
        operator_mode.observe("solo", prompt, True, now=100)
        for tool in ("AskUserQuestion", "ExitPlanMode", "Bash"):
            assert _permission(capsys, tool, "solo") is None
    assert master_coding.asks_operator("ExitPlanMode", "solo")
    assert master_coding.asks_operator("AskUserQuestion", "")
    assert not master_coding.asks_operator("Bash", "solo")


@pytest.mark.parametrize(
    "command",
    [
        "gh api repos/o/r/rulesets -X POST",
        "gh api -X PUT repos/o/r/branches/dev/protection",
        "gh ruleset list",
    ],
)
def test_the_master_never_touches_rulesets_or_branch_protection(master, command):
    assert master_coding.protection_refusal({"tool_input": {"command": command}}) == master_coding.PROTECTION_REFUSAL


def test_ordinary_gh_calls_missing_input_and_other_seats_pass_the_protection_check(master, monkeypatch):
    assert master_coding.protection_refusal({"tool_input": {"command": "gh pr create --base dev"}}) == ""
    assert master_coding.protection_refusal({"tool_name": "Bash"}) == ""
    monkeypatch.setenv("AGENTIHOOKS_SWARM_TASK", "t12")
    assert master_coding.protection_refusal({"tool_input": {"command": "gh api repos/o/r/rulesets"}}) == ""


def test_the_shell_guard_blocks_a_master_ruleset_call(master, monkeypatch):
    monkeypatch.setattr("hooks.context.local_test_guard.check_local_tests", Mock())
    monkeypatch.setattr("hooks.context.swarm_wait_nudge.check_github_watch", Mock())
    monkeypatch.setattr("hooks.context.branch_guard.check_branch_guard", Mock())
    monkeypatch.setattr("hooks.context.branch_guard.check_commit_on_main", Mock())
    with pytest.raises(hook_manager.BlockAction, match="never changes rulesets or branch protection"):
        hook_manager._check_shell_guards({"tool_name": "Bash", "tool_input": {"command": "gh ruleset list"}})
