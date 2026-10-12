import json
import runpy
from pathlib import Path

import pytest

from hooks.context import master_coding, operator_mode

ROOT = Path(__file__).resolve().parents[1]
GUARD = runpy.run_path(str(ROOT / "profiles" / "package" / "roles" / "_guards" / "no_code_edits.py"))
SID = "master-session"


@pytest.fixture
def master(tmp_path, monkeypatch):
    env = {"AGENTIHOOKS_SWARM": "rig", "AGENTIHOOKS_SWARM_TASK": "master", "AGENTIHOOKS_HOME": str(tmp_path / "ah")}
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    return env


def test_only_the_typed_phrase_in_the_master_pane_opens_the_window(master):
    assert master_coding.observe(SID, "allow-master-coding", typed=False) == ""
    assert not master_coding.is_open(SID)
    assert master_coding.observe(SID, "please allow-master-coding now", typed=True, now=1000) == "open"
    assert master_coding.is_open(SID, now=1000 + master_coding.WINDOW_SEC - 1)
    assert not master_coding.is_open(SID, now=1000 + master_coding.WINDOW_SEC)


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


def guard(tmp_path, file_path):
    payload = {"tool_name": "Edit", "tool_input": {"file_path": str(file_path)}, "session_id": SID, "cwd": "/"}
    return GUARD["deny_reason"](payload, "master", tmp_path)


def test_the_window_opens_worktree_edits_but_keeps_conditions_and_credentials_closed(master, tmp_path):
    tree = tmp_path / "dev" / "worktrees" / "agentihooks" / "master-1"
    assert "cannot edit outside" in guard(tmp_path, tree / "hooks" / "x.py")
    master_coding.observe(SID, "allow-master-coding", typed=True)
    assert guard(tmp_path, tree / "hooks" / "x.py") == ""
    assert "stay closed" in guard(tmp_path, tree / ".claude" / "conditions" / "pre-bash-x.py")
    assert "stay closed" in guard(tmp_path, tree / ".env")
    assert guard(tmp_path, tree / ".env.example") == ""
    assert "cannot edit outside" in guard(tmp_path, tmp_path / "dev" / "tcc-ecosystem" / "agentihooks" / "x.py")


def test_an_expired_window_record_no_longer_opens_edits(master, tmp_path):
    record = master_coding.path(SID)
    record.parent.mkdir(parents=True)
    record.write_text(json.dumps({"at": 0}))
    assert "cannot edit outside" in guard(tmp_path, tmp_path / "dev" / "worktrees" / "a" / "b" / "x.py")


def test_the_master_asks_the_present_operator_and_other_seats_stay_denied(master, monkeypatch):
    monkeypatch.setattr(operator_mode, "_heard", lambda session_id, environ, now: True)
    assert master_coding.asks_operator("AskUserQuestion", SID)
    assert master_coding.asks_operator("ExitPlanMode", SID)
    assert not master_coding.asks_operator("Bash", SID)
    monkeypatch.setattr(operator_mode, "_heard", lambda session_id, environ, now: False)
    assert not master_coding.asks_operator("AskUserQuestion", SID)
    monkeypatch.setattr(operator_mode, "_heard", lambda session_id, environ, now: True)
    monkeypatch.setenv("AGENTIHOOKS_SWARM_TASK", "t12")
    assert not master_coding.asks_operator("AskUserQuestion", SID)


@pytest.mark.parametrize(
    "command",
    [
        "gh api repos/o/r/rulesets -X POST",
        "gh api -X PUT repos/o/r/branches/dev/protection",
        "gh ruleset list",
    ],
)
def test_the_master_never_touches_rulesets_or_branch_protection(master, command):
    assert master_coding.protection_refusal(command) == master_coding.PROTECTION_REFUSAL


def test_ordinary_gh_calls_and_other_seats_pass_the_protection_check(master, monkeypatch):
    assert master_coding.protection_refusal("gh pr create --base dev") == ""
    monkeypatch.setenv("AGENTIHOOKS_SWARM_TASK", "t12")
    assert master_coding.protection_refusal("gh api repos/o/r/rulesets") == ""
