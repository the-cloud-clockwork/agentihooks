"""The claim cap: the tick refuses a task's fourth agent life and blocks the task for the master."""

from dataclasses import dataclass

CAP = 3


@dataclass(frozen=True)
class ClaimCap:
    name: str = "claims"
    default_mode: str = "enforce"


GATE = ClaimCap()


def refusal(lives, reason):
    return (
        f"The swarm blocked this task before a fourth agent life: it was claimed {lives} times and the cap is {CAP}. "
        f"Last handoff reason: {reason}. Change its scope or split it, then reopen it for three more lives."
    )
