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


@pytest.fixture
def master(tmp_path, monkeypatch):
    for key, value in {"AGENTIHOOKS_SWARM": "rig", "AGENTIHOOKS_SWARM_TASK": "master"}.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("AGENTIHOOKS_HOME", str(tmp_path / "ah"))
    return tmp_path


def test_only_the_typed_phrase_in_the_master_pane_opens_the_window(master):
    assert master_coding.observe(SID, "allow-master-coding", typed=False) == ""
    assert not master_coding.is_open(SID)
    assert master_coding.observe(SID, "please allow-master-coding now", typed=True, now=1000) == "open"
    assert master_coding.is_open(SID, now=1000 + master_coding.WINDOW_SEC - 1)
    assert not master_coding.is_open(SID, now=1000 + master_coding.WINDOW_SEC)
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
    monkeypatch.delenv("AGENTIHOOKS_HOME")
    monkeypatch.setenv("HOME", str(master))
    assert master_coding.path("s") == master / ".agentihooks" / "master_coding" / "s.json"


def guard(home, file_path):
    payload = {"tool_name": "Edit", "tool_input": {"file_path": str(file_path)}, "session_id": SID, "cwd": "/"}
    return GUARD["deny_reason"](payload, "master", home)


def test_the_window_opens_worktree_edits_but_keeps_conditions_and_credentials_closed(master):
    tree = master / "dev" / "worktrees" / "agentihooks" / "master-1"
    assert "cannot edit outside" in guard(master, tree / "hooks" / "x.py")
    master_coding.observe(SID, "allow-master-coding", typed=True)
    assert guard(master, tree / "hooks" / "x.py") == ""
    assert "stay closed" in guard(master, tree / ".claude" / "conditions" / "pre-bash-x.py")
    assert "stay closed" in guard(master, tree / ".env")
    assert guard(master, tree / ".env.example") == ""
    assert "cannot edit outside" in guard(master, master / "dev" / "tcc-ecosystem" / "agentihooks" / "x.py")


def test_an_expired_window_record_no_longer_opens_edits(master):
    record = master_coding.path(SID)
    record.parent.mkdir(parents=True)
    record.write_text(json.dumps({"at": 0}))
    assert "cannot edit outside" in guard(master, master / "dev" / "worktrees" / "a" / "b" / "x.py")


def test_the_typed_prompt_opens_the_window_and_tells_the_master(master, monkeypatch):
    shown = Mock()
    monkeypatch.setattr("hooks.common.inject_context", shown)
    hook_manager._master_coding({"session_id": SID, "prompt": "allow-master-coding"}, True)
    shown.assert_called_once_with(master_coding.NOTICES["open"], also_log=False, skip_compression=True)
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


def test_the_master_asks_the_present_operator_and_other_seats_stay_denied(master, monkeypatch):
    heard = Mock(return_value=True)
    monkeypatch.setattr(operator_mode, "_heard", heard)
    assert master_coding.asks_operator("AskUserQuestion", SID)
    heard.assert_called_with(SID, None, None)
    assert master_coding.asks_operator("ExitPlanMode", SID)
    assert not master_coding.asks_operator("Bash", SID)
    heard.return_value = False
    assert not master_coding.asks_operator("AskUserQuestion", SID)
    heard.return_value = True
    monkeypatch.setenv("AGENTIHOOKS_SWARM_TASK", "t12")
    assert not master_coding.asks_operator("AskUserQuestion", SID)


def test_the_permission_request_passes_to_the_present_operator_only_from_the_master(master, monkeypatch, capsys):
    heard = Mock(return_value=True)
    monkeypatch.setattr(operator_mode, "_heard", heard)
    hook_manager.on_permission_request({"tool_name": "AskUserQuestion", "session_id": SID})
    assert capsys.readouterr().out == ""
    heard.assert_called_with(SID, None, None)
    monkeypatch.setenv("AGENTIHOOKS_SWARM_TASK", "t12")
    hook_manager.on_permission_request({"tool_name": "AskUserQuestion", "session_id": SID})
    assert "swarm sessions and subagents cannot wait for permission" in capsys.readouterr().out


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
