"""Retry policy of the lab: one retry for a lost worker, never for a failed test."""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from scripts.ci_lab.outcomes import LOSSES, PASSED, UNGRADED

__all__ = ["Policy", "UnitResult", "decide", "policy", "red"]


@dataclass(frozen=True)
class Policy:
    """Retries a unit may take: lost for a lost lease or a crashed worker, failed for a graded failure."""

    lost: int = 1
    failed: int = 0


@dataclass(frozen=True)
class UnitResult:
    """One run of a unit: its outcome and how many times the unit already ran again.

    Outcomes are those scripts.ci_lab.outcomes names: passed; lost (an expired lease) and crash (a crashed worker)
    are losses; failed, gate_failed, survived and cannot_run are failures.
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
    """Return whether the result, once final, makes its unit's grade red: a failure with a verdict does, and neither
    a pass nor a result that ends without a verdict, which leaves the grade neutral."""
    return result.outcome != PASSED and result.outcome not in UNGRADED


def policy(lab: Mapping[str, Any]) -> Policy:
    """Return the retry counts of a validated lab block, the defaults for any count lab.retry leaves out."""
    return Policy(**lab.get("retry", {}))
