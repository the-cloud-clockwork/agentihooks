"""The acceptance rule report: GitHub Actions jobs against lab-shadow check runs over the last heads of one gate."""

import subprocess
from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, cast

import scripts.ci_budget.rate_limit

__all__ = [
    "Head",
    "Rule",
    "Tally",
    "Verdict",
    "actions",
    "claims",
    "collect",
    "first",
    "head",
    "holds",
    "lab",
    "main",
    "mismatch",
    "render",
    "rerun",
    "rule",
    "tally",
]

gh_api = cast("Callable[..., subprocess.CompletedProcess[str]]", scripts.ci_budget.rate_limit.gh_api)

ROOT = Path(__file__).resolve().parents[2]

Fetch = Callable[[str], Mapping[str, Any]]


@dataclass(frozen=True)
class Verdict:
    """One side's verdict on a gate of a head, pass or fail, and when its last graded check run completed."""

    outcome: str
    completed_at: datetime


@dataclass(frozen=True)
class Head:
    """One head's Actions verdict on a gate and its lab verdict, None when the lab posted no completed verdict."""

    sha: str
    actions: Verdict
    lab: Verdict | None


@dataclass(frozen=True)
class Rule:
    """The acceptance rule over a window of heads: how many heads, lab verdicts present and lab verdicts first."""

    heads: int
    present: int
    first: int


@dataclass(frozen=True)
class Tally:
    """The counts the rule judges: heads, lab verdicts present, mismatches and lab verdicts first."""

    heads: int
    present: int
    mismatches: int
    first: int


def rule(heads: int) -> Rule:
    """Return the rule for a window of heads."""
    return Rule(heads, 0, 0)


def rerun(name: str) -> str | None:
    """Return the name of the job a unit job's lost runner retry runs under."""
    return None


def actions(runs: Sequence[Mapping[str, Any]], gate: str, static: Collection[str]) -> Verdict | None:
    """Return the Actions verdict of the gate's jobs on a head."""
    return None


def lab(runs: Sequence[Mapping[str, Any]], gate: str) -> Verdict | None:
    """Return the lab-shadow verdict on the gate."""
    return None


def head(sha: str, runs: Sequence[Mapping[str, Any]], gate: str, static: Collection[str]) -> Head | None:
    """Return the head's verdicts on the gate."""
    return None


def mismatch(found: Head) -> bool:
    """Whether the lab and Actions disagree on the head."""
    return False


def first(found: Head) -> bool:
    """Whether the lab verdict completed before the Actions jobs."""
    return False


def tally(heads: Sequence[Head]) -> Tally:
    """Return the counts the rule judges."""
    return Tally(0, 0, 0, 0)


def holds(counted: Tally, window: Rule) -> bool:
    """Whether the counts meet the rule."""
    return False


def render(gate: str, event: str, heads: Sequence[Head], window: Rule) -> str:
    """Return the report table."""
    return ""


def collect(fetch: Fetch, repository: str, event: str, gate: str, static: Collection[str], count: int) -> list[Head]:
    """Return the newest heads of the event with an Actions verdict on the gate."""
    return []


def claims(argv: Sequence[str]) -> bool:
    """Whether a shadow command is the acceptance rule report."""
    return False


def main(argv: Sequence[str], environ: Mapping[str, str], fetch: Fetch | None = None) -> int:
    """Print the acceptance rule report of one gate."""
    return 0
