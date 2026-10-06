import json

import pytest

import hooks.hook_manager as hm
from hooks.context import planner_plan
from hooks.targets import emitter

PLAN = {"plan": "1. Slice the phase.", "planFilePath": "/home/u/.claude/plans/p.md"}


@pytest.mark.parametrize(
    ("environ", "tool", "accepted"),
    [
        ({"AGENTIHOOKS_SWARM": "sw", "AGENTIHOOKS_SWARM_LANE": "plan"}, "ExitPlanMode", True),
        ({"AGENTIHOOKS_SWARM": "sw", "AGENTIHOOKS_SWARM_LANE": "eng"}, "ExitPlanMode", False),
        ({"AGENTIHOOKS_SWARM": "sw", "AGENTIHOOKS_SWARM_LANE": "master"}, "ExitPlanMode", False),
        ({"AGENTIHOOKS_SWARM_LANE": "plan"}, "ExitPlanMode", False),
        ({"AGENTIHOOKS_SWARM": "", "AGENTIHOOKS_SWARM_LANE": "plan"}, "ExitPlanMode", False),
        ({"AGENTIHOOKS_SWARM": "sw", "AGENTIHOOKS_SWARM_LANE": "plan"}, "Write", False),
    ],
)
def test_only_a_swarm_planner_leaving_plan_mode_is_accepted(environ, tool, accepted):
    assert planner_plan.accepts(tool, environ, "claude") is accepted


def test_codex_has_no_plan_exit_tool_to_accept():
    assert not planner_plan.accepts(
        "ExitPlanMode", {"AGENTIHOOKS_SWARM": "sw", "AGENTIHOOKS_SWARM_LANE": "plan"}, "codex"
    )


def test_the_live_environment_and_target_are_the_defaults(monkeypatch):
    monkeypatch.setenv("AGENTIHOOKS_TARGET", "claude")
    monkeypatch.setenv("AGENTIHOOKS_SWARM", "sw")
    monkeypatch.setenv("AGENTIHOOKS_SWARM_LANE", "plan")
    assert planner_plan.accepts("ExitPlanMode")


@pytest.fixture
def forced_claude(monkeypatch):
    monkeypatch.setenv("AGENTIHOOKS_TARGET", "claude")
    monkeypatch.setattr(emitter, "_forced", True)
    monkeypatch.setattr(emitter, "_buffer", [])


def _pre_tool_use(tmp_path, capsys, tool="ExitPlanMode", tool_input=PLAN):
    hm.on_pre_tool_use(
        {
            "hook_event_name": "PreToolUse",
            "session_id": "sid-planner-plan",
            "tool_name": tool,
            "tool_input": dict(tool_input),
            "permission_mode": "plan",
            "cwd": str(tmp_path),
        }
    )
    out = capsys.readouterr().out.strip()
    return json.loads(out)["hookSpecificOutput"] if out else {}


def test_a_swarm_planner_leaves_plan_mode_with_its_plan_as_the_updated_input(
    forced_claude, monkeypatch, capsys, tmp_path
):
    monkeypatch.setenv("AGENTIHOOKS_SWARM", "sw")
    monkeypatch.setenv("AGENTIHOOKS_SWARM_LANE", "plan")
    out = _pre_tool_use(tmp_path, capsys)
    assert out["permissionDecision"] == "allow"
    assert out["updatedInput"] == PLAN
    assert out["permissionDecisionReason"] == planner_plan.REASON


def test_an_engineer_leaving_plan_mode_still_gets_the_pane_prompt(forced_claude, monkeypatch, capsys, tmp_path):
    monkeypatch.setenv("AGENTIHOOKS_SWARM", "sw")
    monkeypatch.setenv("AGENTIHOOKS_SWARM_LANE", "eng")
    assert "permissionDecision" not in _pre_tool_use(tmp_path, capsys)
