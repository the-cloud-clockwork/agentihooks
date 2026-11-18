# pyright: reportUnusedImport=false
"""Runner cost and lead time over one window, measured the way the 4 November CI at agent scale report measured them.

Runner minutes are the summed run time of every job that was not skipped. A workflow's runs whose jobs were not read
are counted at the minutes per run of its read runs. A merged pull request's lead time runs from the first CI run on
its branch to its merge.
"""

from collections import defaultdict  # noqa: F401
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from scripts import ci_minutes  # noqa: F401
from scripts.swarm import metrics_outbox

CANCELLED = "cancelled"
SKIPPED = "skipped"
WINDOW = "window"
TIER = "tier"
POOL = "pool"
ALL = "all"
COST = metrics_outbox.Table(
    "ci_cost",
    (
        ("scope", "String"),
        ("name", "String"),
        ("runs", "Int64"),
        ("read_runs", "Int64"),
        ("runner_minutes", "Float64"),
        ("estimated_minutes", "Float64"),
        ("cancelled_runs", "Int64"),
        ("cancelled_minutes", "Float64"),
        ("merged", "Int64"),
        ("per_merged", "Float64"),
        ("pending_runners", "Float64"),
    ),
)
LEAD = metrics_outbox.Table(
    "ci_lead_time",
    (
        ("pr", "Int64"),
        ("branch", "String"),
        ("first_run_ms", "Int64"),
        ("merged_ms", "Int64"),
        ("minutes", "Float64"),
        ("heads", "Int64"),
        ("runs", "Int64"),
    ),
)


@dataclass(frozen=True)
class Tally:
    """Runner minutes of a set of runs and the part that cancelled runs spent."""

    runs: int = 0
    read: int = 0
    minutes: float = 0.0
    estimated: float = 0.0
    cancelled_runs: int = 0
    cancelled_minutes: float = 0.0

    def __add__(self, other: "Tally") -> "Tally":
        """Sum two tallies field by field."""
        raise NotImplementedError()


@dataclass(frozen=True)
class Lead:
    """One merged pull request, from the first CI run on its branch to its merge."""

    number: int
    branch: str
    first_run: datetime
    merged: datetime
    heads: int
    runs: int

    @property
    def minutes(self) -> float:
        """Minutes from the first CI run on the branch to the merge."""
        raise NotImplementedError()


def at(stamp: str) -> datetime:
    """Parse a GitHub UTC timestamp."""
    raise NotImplementedError()


def job_minutes(job: dict[str, Any]) -> float:
    """Runner minutes of one job; a skipped job spends none."""
    raise NotImplementedError()


def minutes_by_run(jobs: Iterable[dict[str, Any]]) -> dict[int, float]:
    """Runner minutes per run id, each job counted once by its id."""
    raise NotImplementedError()


def tally(runs: Sequence[dict[str, Any]], spent: Mapping[int, float]) -> Tally:
    """Tally one workflow's runs; its unread runs count at the minutes per run of its read runs."""
    raise NotImplementedError()


def tallies(runs: Iterable[dict[str, Any]], jobs: Iterable[dict[str, Any]]) -> dict[tuple[str, str], Tally]:
    """Tally the runs of each tier and workflow file."""
    raise NotImplementedError()


def by_tier(groups: Mapping[tuple[str, str], Tally]) -> dict[str, Tally]:
    """Sum workflow tallies into their tiers."""
    raise NotImplementedError()


def lead(pull: Mapping[str, Any], runs: Sequence[Mapping[str, Any]], opened: datetime) -> Lead | None:
    """Lead time of one merged pull request over its branch's runs created up to the merge.

    A branch whose first run is not after `opened`, the start of the run record, or comes after the merge has no lead
    time, since its first run is unknown."""
    raise NotImplementedError()


def lead_times(pulls: Iterable[Mapping[str, Any]], runs: Iterable[Mapping[str, Any]], opened: datetime) -> list[Lead]:
    """Lead times of the merged pull requests whose first branch run is known."""
    raise NotImplementedError()


def percentile(values: Sequence[float], share: float) -> float | None:
    """Linear interpolation between the closest ranks, as the 4 November report computed it."""
    raise NotImplementedError()


def ratio(part: float, whole: float) -> float:
    """Part over whole, zero for an empty whole."""
    raise NotImplementedError()


def rounded(value: float | None, digits: int) -> float | None:
    """Round a value that may be missing."""
    raise NotImplementedError()


def tier_summary(part: Tally, merged: int) -> dict[str, Any]:
    """Printed view of one tally."""
    raise NotImplementedError()


def summary(
    tiers: Mapping[str, Tally], merged: int, leads: Sequence[Lead], pending: Mapping[str, float]
) -> dict[str, Any]:
    """Headline numbers of one window: cost per tier and per merged pull request, lead time, heads, pending runners."""
    raise NotImplementedError()


def cost_row(
    repo: str, start: datetime, key: tuple[str, str], part: Tally, merged: int, pending: float
) -> dict[str, Any]:
    """One ci_cost row for a window, tier or runner pool."""
    raise NotImplementedError()


def lead_row(repo: str, item: Lead) -> dict[str, Any]:
    """One ci_lead_time row for a merged pull request."""
    raise NotImplementedError()


def rows(
    repo: str,
    start: datetime,
    tiers: Mapping[str, Tally],
    merged: int,
    leads: Sequence[Lead],
    pending: Mapping[str, float],
) -> dict[metrics_outbox.Table, list[dict[str, Any]]]:
    """Outbox rows of one window: the window total, each tier, each runner pool, and each merged pull request."""
    raise NotImplementedError()
