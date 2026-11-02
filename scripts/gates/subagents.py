"""The sub-agent budget: every sub-agent launch and continuation in a swarm task is counted, whatever its name."""

from typing import Any, cast

from scripts.gates import log
from scripts.gates.base import Call, Decision, Who
from scripts.gates.budget import Budget

LAUNCH_TOOLS = frozenset({"Agent", "Task"})
CONTINUE_TOOLS = frozenset({"SendMessage"})
READERS = ("standards-reader", "spec-reader")


def refusal(slug: str, task: str, counter: str, cap: int) -> str:
    return (
        f"the sub-agent budget for task {task} is spent: {cap} of {cap} {counter}. Merge with the ruled findings, "
        f'file the spec finding as a follow-up, or block with agentihooks swarm {slug} block "<why>"'
    )


class SubagentBudget:
    name = "subagents"
    default_mode = "enforce"

    def __init__(self, launches: int = 8, continuations: int = 8) -> None:
        self.launches, self.continuations = launches, continuations

    def matches(self, call: Call) -> bool:
        return call.tool in LAUNCH_TOOLS or call.tool in CONTINUE_TOOLS

    def decide(self, call: Call, who: Who, state: Any) -> Decision:
        if not (who.pinned and who.task):
            return Decision()
        counter, cap = (
            ("launches", self.launches) if call.tool in LAUNCH_TOOLS else ("continuations", self.continuations)
        )
        budget = Budget(state.slug, self.name, state.home)
        allowed, spent = budget.spend(who.task, counter, cap)
        if not allowed:
            return Decision.deny(refusal(who.swarm, who.task, counter, cap))
        if call.tool in LAUNCH_TOOLS:
            for reader in {call.tool_input.get("name"), call.tool_input.get("subagent_type")}.intersection(READERS):
                budget.spend(who.task, cast(str, reader), 1)
        reason = f"sub-agent {counter} {spent} of {cap} for task {who.task}"
        log.append(state.slug, log.Row.of(self.name, "count", who, call.tool, reason), state.home)
        return Decision()
