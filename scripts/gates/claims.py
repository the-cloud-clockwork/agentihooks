"""The claim cap: the tick refuses a task's fourth agent life and blocks the task for the master."""

from dataclasses import dataclass

CAP = 3


@dataclass(frozen=True)
class ClaimCap:
    name: str = "claims"
    default_mode: str = "enforce"


GATE = ClaimCap()


def refusal(lives, reason, failure, slug, task):
    return (
        f"The swarm blocked this task before a fourth agent life: claimed {lives} times, cap {CAP}. "
        f"Last handoff reason: {reason}. Last launch failure: {failure}. Change its scope or split it, then reopen "
        f"it for three more lives with agentihooks ledger --slug {slug} task set {task} state=open"
    )
