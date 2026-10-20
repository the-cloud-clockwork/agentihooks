"""The claim cap: the tick refuses a task's fourth agent life and blocks the task for the master."""

from dataclasses import dataclass
from datetime import UTC, datetime

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


def launch_failure(lives: int, launches: list[dict], task: str) -> str:
    rows = [row for row in launches if row["task"] == task]
    starts = sorted(row["at"] for row in rows if row["state"] == "started")
    if not starts:
        return "none"
    first = starts[-lives:][0]
    failures = [row for row in rows if row["state"] == "failed" and row["at"] >= first]
    if not failures:
        return "none"
    last = max(failures, key=lambda row: row["at"])
    at = datetime.fromtimestamp(last["at"] / 1000, UTC).strftime("%Y-%m-%d %H:%M:%S UTC")
    return f"{at}: {last['error']}"
