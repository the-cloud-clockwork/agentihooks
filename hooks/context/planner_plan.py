"""A swarm planner leaves plan mode without a pane prompt: the ledger plan review approves its plan."""

import os
from collections.abc import Mapping
from typing import Any

from hooks.targets.capabilities import plan_accept_tools

BYPASS = {"type": "setMode", "mode": "bypassPermissions", "destination": "session"}


def accepts(tool_name: str, environ: Mapping[str, str] | None = None, target: str | None = None) -> bool:
    env = os.environ if environ is None else environ
    return (
        bool(env.get("AGENTIHOOKS_SWARM"))
        and env.get("AGENTIHOOKS_SWARM_LANE") == "plan"
        and tool_name in plan_accept_tools(target)
    )


def grant(tool_input: dict[str, Any]) -> dict[str, Any]:
    """Allow with the tool input returned, since Claude Code otherwise keeps its plan prompt and a plan mode session falls back to manual mode on exit."""
    decision = {"behavior": "allow", "updatedInput": tool_input, "updatedPermissions": [BYPASS]}
    return {"hookSpecificOutput": {"hookEventName": "PermissionRequest", "decision": decision}}
