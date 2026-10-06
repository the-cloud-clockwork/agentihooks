"""A swarm planner leaves plan mode without a pane prompt: the ledger plan review approves its plan."""

import os

from hooks.targets.capabilities import plan_accept_tools

BYPASS = {"type": "setMode", "mode": "bypassPermissions", "destination": "session"}


def accepts(tool_name: str, environ=None, target: str | None = None) -> bool:
    env = os.environ if environ is None else environ
    return (
        bool(env.get("AGENTIHOOKS_SWARM"))
        and env.get("AGENTIHOOKS_SWARM_LANE") == "plan"
        and tool_name in plan_accept_tools(target)
    )


def grant(tool_input: dict) -> dict:
    # Claude Code keeps its plan prompt for an allow that does not return the tool input, and a session started in
    # plan mode falls back to manual mode on exit.
    decision = {"behavior": "allow", "updatedInput": tool_input, "updatedPermissions": [BYPASS]}
    return {"hookSpecificOutput": {"hookEventName": "PermissionRequest", "decision": decision}}
