import json

import pytest

from hooks import hook_manager as hm


@pytest.mark.parametrize("target", ["claude", "codex", "copilot"])
@pytest.mark.parametrize("lane", ["eng", "ci", "plan", "master", ""])
@pytest.mark.parametrize("subagent", [False, True])
def test_swarm_permission_requests_are_denied_without_a_prompt(monkeypatch, capsys, target, lane, subagent):
    monkeypatch.setenv("AGENTIHOOKS_TARGET", target)
    monkeypatch.setenv("AGENTIHOOKS_SWARM", "swarm")
    monkeypatch.setenv("AGENTIHOOKS_SWARM_LANE", lane)
    payload = {
        "hook_event_name": "PermissionRequest",
        "tool_name": "Bash",
        "tool_input": {"command": "date"},
        "permission_mode": "bypassPermissions",
    }
    if subagent:
        payload["agent_id"] = "restricted-reviewer"
        payload["agent_type"] = "code-reviewer"

    hm.EVENT_HANDLERS["PermissionRequest"](payload)

    reason = (
        "Denied Bash: swarm sessions and subagents cannot wait for permission. "
        "Use an allowed tool or report the missing capability through the swarm inbox."
    )
    output = json.loads(capsys.readouterr().out)
    if target == "claude":
        assert output == {
            "hookSpecificOutput": {
                "hookEventName": "PermissionRequest",
                "decision": {"behavior": "deny", "message": reason},
            }
        }
    elif target == "codex":
        assert output == {
            "hookSpecificOutput": {
                "hookEventName": "PermissionRequest",
                "permissionDecision": "deny",
                "permissionDecisionReason": reason,
            }
        }
    else:
        assert output == {"decision": "block", "reason": reason}


@pytest.mark.parametrize("swarm", [None, ""])
def test_non_swarm_permission_requests_keep_their_existing_behavior(monkeypatch, capsys, swarm):
    monkeypatch.setenv("AGENTIHOOKS_TARGET", "claude")
    if swarm is None:
        monkeypatch.delenv("AGENTIHOOKS_SWARM", raising=False)
    else:
        monkeypatch.setenv("AGENTIHOOKS_SWARM", swarm)
    hm.EVENT_HANDLERS["PermissionRequest"]({"tool_name": "Bash", "tool_input": {"command": "date"}})
    assert capsys.readouterr().out == ""
