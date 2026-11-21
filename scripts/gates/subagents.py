"""The sub-agent budget: every sub-agent launch and continuation in a swarm task is counted, whatever its name."""

import sys
from typing import TYPE_CHECKING

from scripts.gates import lift, log, recon
from scripts.gates.base import Call, Decision, Who
from scripts.gates.budget import Budget
from scripts.swarm_ledger import ledger_workspace

if TYPE_CHECKING:
    from scripts.gates.verdicts import Verdicts

LAUNCH_TOOLS = frozenset({"Agent", "Task"})
CONTINUE_TOOLS = frozenset({"SendMessage"})
READERS = ("standards-reader", "spec-reader")
SCOUTER = "scouter"


def refusal(slug, task, counter, cap):
    return (
        f"the sub-agent budget for task {task} is spent: {cap} of {cap} {counter}. Merge with the ruled findings, "
        f'file the spec finding as a follow-up, or block with agentihooks swarm {slug} block "<why>"'
    )


class SubagentBudget:
    name = "subagents"
    default_mode = "enforce"

    def __init__(self, launches=8, continuations=8):
        self.launches, self.continuations = launches, continuations

    def matches(self, call):
        return call.tool in LAUNCH_TOOLS or call.tool in CONTINUE_TOOLS

    def decide(self, call: Call, who: Who, state: "Verdicts") -> Decision:
        if not (who.pinned and who.task):
            return Decision()
        kind = call.tool_input.get("subagent_type")  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
        if (
            call.tool in LAUNCH_TOOLS
            and kind == SCOUTER
            and not recon.valid(ledger_workspace.folder(who.swarm, who.task))
        ):
            reason = f"recon scouter launch for task {who.task}, not counted before its recon plan is valid"
            row = log.Row.of(self.name, "count", who, call.tool, reason)  # pyright: ignore[reportUnknownMemberType]
            log.append(state.slug, row, state.home)  # pyright: ignore[reportUnknownMemberType, reportUnknownArgumentType]
            return Decision()
        counter, cap = (
            ("launches", self.launches) if call.tool in LAUNCH_TOOLS else ("continuations", self.continuations)
        )
        budget = Budget(state.slug, self.name, state.home)
        allowed, spent = budget.spend(who.task, counter, cap)
        named = {call.tool_input.get("name"), call.tool_input.get("subagent_type")}
        readers = [reader for reader in READERS if reader in named] if call.tool in LAUNCH_TOOLS else []
        if readers and (allowed or lift.head_lifted(state.slug, who.task, self.name, state.home)):
            for reader in readers:
                budget.spend(who.task, reader, 1)
            record_on_ledger(who, readers)
        if not allowed:
            return Decision.deny(refusal(who.swarm, who.task, counter, cap))
        reason = f"sub-agent {counter} {spent} of {cap} for task {who.task}"
        log.append(state.slug, log.Row.of(self.name, "count", who, call.tool, reason), state.home)
        return Decision()


def record_on_ledger(who: Who, readers: list[str]) -> None:
    """Record each reader launch on the ledger task so a successor on another hive sees it ran; a reader the ledger
    cannot take stays counted only on this machine, says so on stderr, and leaves the next reader still tried."""
    from scripts.swarm import review_rounds
    from scripts.swarm.ledger_client import LedgerClient
    from scripts.swarm.store import SwarmError

    ledger = LedgerClient()
    for reader in readers:
        try:
            review_rounds.record_reader(who.swarm, who.task, reader, ledger, who.name)
        except (SwarmError, OSError) as exc:
            print(
                f"reader launch counted on this machine only, the ledger task did not take it: {exc}", file=sys.stderr
            )
