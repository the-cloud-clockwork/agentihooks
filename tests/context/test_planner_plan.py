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


def _hook(event, tmp_path, capsys, tool="ExitPlanMode"):
    handler = hm.on_permission_request if event == "PermissionRequest" else hm.on_pre_tool_use
    handler(
        {
            "hook_event_name": event,
            "session_id": "sid-planner-plan",
            "tool_name": tool,
            "tool_input": dict(PLAN),
            "permission_mode": "plan",
            "cwd": str(tmp_path),
        }
    )
    out = capsys.readouterr().out.strip()
    return json.loads(out)["hookSpecificOutput"] if out else {}


def _planner(monkeypatch, lane="plan"):
    monkeypatch.setenv("AGENTIHOOKS_SWARM", "sw")
    monkeypatch.setenv("AGENTIHOOKS_SWARM_LANE", lane)


def test_a_swarm_planner_leaves_plan_mode_into_bypass_with_its_plan_as_the_updated_input(
    forced_claude, monkeypatch, capsys, tmp_path
):
    _planner(monkeypatch)
    assert _hook("PermissionRequest", tmp_path, capsys) == {
        "hookEventName": "PermissionRequest",
        "decision": {
            "behavior": "allow",
            "updatedInput": PLAN,
            "updatedPermissions": [{"type": "setMode", "mode": "bypassPermissions", "destination": "session"}],
        },
    }


def test_pre_tool_use_leaves_the_planner_plan_exit_to_the_permission_request(
    forced_claude, monkeypatch, capsys, tmp_path
):
    _planner(monkeypatch)
    assert "permissionDecision" not in _hook("PreToolUse", tmp_path, capsys)


@pytest.mark.parametrize(("lane", "tool"), [("eng", "ExitPlanMode"), ("plan", "Bash")])
def test_anything_else_still_gets_the_pane_prompt(forced_claude, monkeypatch, capsys, tmp_path, lane, tool):
    _planner(monkeypatch, lane)
    assert _hook("PermissionRequest", tmp_path, capsys, tool) == {}
