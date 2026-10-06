"""The sub-agent budget: every sub-agent launch and continuation in a swarm task is counted, whatever its name."""

from scripts.gates import log
from scripts.gates.base import Decision
from scripts.gates.budget import Budget

LAUNCH_TOOLS = frozenset({"Agent", "Task"})
CONTINUE_TOOLS = frozenset({"SendMessage"})


def refusal(slug, task, counter, cap):
    return (
        f"the sub-agent budget for task {task} is spent: {cap} of {cap} {counter}. Merge with the ruled findings, "
        f'file the spec finding as a follow-up, or block with agentihooks swarm {slug} block "<why>"'
    )


class SubagentBudget:
    name = "subagents"
    default_mode = "observe"

    def __init__(self, launches=8, continuations=8):
        self.launches, self.continuations = launches, continuations

    def matches(self, call):
        return call.tool in LAUNCH_TOOLS or call.tool in CONTINUE_TOOLS

    def decide(self, call, who, state):
        if not (who.pinned and who.task):
            return Decision()
        counter, cap = (
            ("launches", self.launches) if call.tool in LAUNCH_TOOLS else ("continuations", self.continuations)
        )
        allowed, spent = Budget(state.slug, self.name, state.home).spend(who.task, counter, cap)
        if not allowed:
            return Decision.deny(refusal(who.swarm, who.task, counter, cap))
        reason = f"sub-agent {counter} {spent} of {cap} for task {who.task}"
        log.append(state.slug, log.Row.of(self.name, "count", who, call.tool, reason), state.home)
        return Decision()
