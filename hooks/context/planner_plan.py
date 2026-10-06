"""A swarm planner leaves plan mode without a pane prompt: the ledger plan review approves its plan."""

import os

from hooks.targets.capabilities import plan_accept_tools

REASON = "Swarm planner: the ledger plan review approves this plan, not the pane."


def accepts(tool_name: str, environ=None, target: str | None = None) -> bool:
    env = os.environ if environ is None else environ
    return (
        bool(env.get("AGENTIHOOKS_SWARM"))
        and env.get("AGENTIHOOKS_SWARM_LANE") == "plan"
        and tool_name in plan_accept_tools(target)
    )
