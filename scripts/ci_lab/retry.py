"""Retry policy of the lab: one retry for a lost worker, never for a failed test."""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from scripts.ci_lab.outcomes import LOSSES, PASSED

__all__ = ["Policy", "UnitResult", "decide", "policy", "red"]


@dataclass(frozen=True)
class Policy:
    """Retries a unit may take: lost for a lost lease or a crashed worker, failed for a graded failure."""

    lost: int = 1
    failed: int = 0


@dataclass(frozen=True)
class UnitResult:
    """One run of a unit: its outcome and how many times the unit already ran again.

    Outcomes: passed; lost (an expired lease) and crash (a crashed worker) are losses; failed (a test failure),
    gate_failed (a static gate failure), survived (a survived mutant) and any other outcome are failures.
    """

    outcome: str
    attempts: int


DEFAULT = Policy()


def decide(result: UnitResult, rules: Policy = DEFAULT) -> str:
    """Return retry while the result's kind has retries left under the rules, else final."""
    if result.outcome == PASSED:
        return "final"
    allowed = rules.lost if result.outcome in LOSSES else rules.failed
    return "retry" if result.attempts < allowed else "final"


def red(result: UnitResult) -> bool:
    """Return whether the result, once final, makes its unit's grade red."""
    return result.outcome != PASSED


def policy(lab: Mapping[str, Any]) -> Policy:
    """Return the retry counts of a validated lab block, the defaults for any count lab.retry leaves out."""
    return Policy(**lab.get("retry", {}))
