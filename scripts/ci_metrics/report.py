"""Runner cost and lead time of agentihooks CI over one window.

Runner minutes are the summed run time of every job that was not skipped. A workflow's runs whose jobs were not read
are counted at the minutes per run of its read runs. A merged pull request's lead time runs from the first CI run on
its branch to its merge.
"""

from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from scripts import ci_minutes
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
        return Tally(
            self.runs + other.runs,
            self.read + other.read,
            self.minutes + other.minutes,
            self.estimated + other.estimated,
            self.cancelled_runs + other.cancelled_runs,
            self.cancelled_minutes + other.cancelled_minutes,
        )


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
        return (self.merged - self.first_run).total_seconds() / 60


def at(stamp: str) -> datetime:
    """Parse a GitHub UTC timestamp."""
    return datetime.fromisoformat(stamp)


def job_minutes(job: dict[str, Any]) -> float:
    """Runner minutes of one job; a skipped job spends none."""
    if job.get("conclusion") == SKIPPED:
        return 0.0
    return ci_minutes.seconds(job) / 60


def minutes_by_run(jobs: Iterable[dict[str, Any]]) -> dict[int, float]:
    """Runner minutes per run id, each job counted once by its id."""
    unique = {job["id"]: job for job in jobs}
    spent: defaultdict[int, float] = defaultdict(float)
    for job in unique.values():
        spent[job["run_id"]] += job_minutes(job)
    return dict(spent)


def tally(runs: Sequence[dict[str, Any]], spent: Mapping[int, float]) -> Tally:
    """Tally one workflow's runs; its unread runs count at the minutes per run of its read runs."""
    read = [run for run in runs if run["id"] in spent]
    minutes = sum(spent[run["id"]] for run in read)
    cancelled = [run for run in runs if run["conclusion"] == CANCELLED]
    return Tally(
        runs=len(runs),
        read=len(read),
        minutes=minutes,
        estimated=minutes / len(read) * len(runs) if read else 0.0,
        cancelled_runs=len(cancelled),
        cancelled_minutes=sum(spent.get(run["id"], 0.0) for run in cancelled),
    )


def tallies(runs: Iterable[dict[str, Any]], jobs: Iterable[dict[str, Any]]) -> dict[tuple[str, str], Tally]:
    """Tally the runs of each tier and workflow file."""
    spent = minutes_by_run(jobs)
    groups: defaultdict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for run in runs:
        groups[(ci_minutes.tier(run), run["path"])].append(run)
    return {key: tally(group, spent) for key, group in groups.items()}


def by_tier(groups: Mapping[tuple[str, str], Tally]) -> dict[str, Tally]:
    """Sum workflow tallies into their tiers."""
    tiers: defaultdict[str, Tally] = defaultdict(Tally)
    for (name, _), part in groups.items():
        tiers[name] = tiers[name] + part
    return dict(tiers)


def lead(pull: Mapping[str, Any], runs: Sequence[Mapping[str, Any]], opened: datetime) -> Lead | None:
    """Lead time of one merged pull request over its branch's runs created up to the merge.

    A branch whose first run is not after `opened`, the start of the run record, or comes after the merge has no lead
    time, since its first run is unknown.
    """
    merged = at(pull["merged_at"])
    own = [run for run in runs if at(run["created_at"]) <= merged]
    if not own:
        return None
    first = min(at(run["created_at"]) for run in own)
    if first <= opened:
        return None
    return Lead(pull["number"], pull["branch"], first, merged, len({run["head_sha"] for run in own}), len(own))


def lead_times(pulls: Iterable[Mapping[str, Any]], runs: Iterable[Mapping[str, Any]], opened: datetime) -> list[Lead]:
    """Lead times of the merged pull requests whose first branch run is known."""
    branches: defaultdict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for run in runs:
        branches[run["head_branch"]].append(run)
    found = (lead(pull, branches.get(pull["branch"], []), opened) for pull in pulls)
    return [item for item in found if item is not None]


def percentile(values: Sequence[float], share: float) -> float | None:
    """Linear interpolation between the closest ranks."""
    if not values:
        return None
    ordered = sorted(values)
    rank = (len(ordered) - 1) * share
    low = int(rank)
    high = min(low + 1, len(ordered) - 1)
    return ordered[low] + (ordered[high] - ordered[low]) * (rank - low)


def ratio(part: float, whole: float) -> float:
    """Part over whole, zero for an empty whole."""
    return part / whole if whole else 0.0


def rounded(value: float | None, digits: int) -> float | None:
    """Round a value that may be missing."""
    return None if value is None else round(value, digits)


def tier_summary(part: Tally, merged: int) -> dict[str, Any]:
    """Printed view of one tally."""
    return {
        "runs": part.runs,
        "read_runs": part.read,
        "runner_minutes": round(part.estimated, 1),
        "per_merged": round(ratio(part.estimated, merged), 1),
        "cancelled_share": round(ratio(part.cancelled_runs, part.runs), 3),
        "cancelled_minutes": round(part.cancelled_minutes, 1),
        "cancelled_minutes_share": round(ratio(part.cancelled_minutes, part.minutes), 3),
    }


def summary(
    tiers: Mapping[str, Tally], merged: int, leads: Sequence[Lead], pending: Mapping[str, float]
) -> dict[str, Any]:
    """Headline numbers of one window: cost per tier and per merged pull request, lead time, heads, pending runners."""
    minutes = [item.minutes for item in leads]
    heads = [float(item.heads) for item in leads]
    return {
        "merged": merged,
        **tier_summary(sum(tiers.values(), Tally()), merged),
        "tiers": {name: tier_summary(part, merged) for name, part in sorted(tiers.items())},
        "lead_time": {
            "pull_requests": len(leads),
            "p50_minutes": rounded(percentile(minutes, 0.5), 1),
            "p90_minutes": rounded(percentile(minutes, 0.9), 1),
        },
        "heads": {
            "p50": percentile(heads, 0.5),
            "p90": percentile(heads, 0.9),
            "max": max(heads, default=None),
        },
        "pending_runners": {pool: round(value, 2) for pool, value in sorted(pending.items())},
    }


def cost_row(
    repo: str, start: datetime, key: tuple[str, str], part: Tally, merged: int, pending: float
) -> dict[str, Any]:
    """One ci_cost row for a window, tier or runner pool."""
    scope, name = key
    return {
        "event_id": f"ci-cost:{repo}:{start.isoformat()}:{scope}:{name}",
        "ledger": repo,
        "ts_ms": round(start.timestamp() * 1000),
        "plan": "",
        "phase": "",
        "slice": "",
        "task": "",
        "scope": scope,
        "name": name,
        "runs": part.runs,
        "read_runs": part.read,
        "runner_minutes": float(part.minutes),
        "estimated_minutes": float(part.estimated),
        "cancelled_runs": part.cancelled_runs,
        "cancelled_minutes": float(part.cancelled_minutes),
        "merged": merged,
        "per_merged": ratio(part.estimated, merged),
        "pending_runners": float(pending),
    }


def lead_row(repo: str, item: Lead) -> dict[str, Any]:
    """One ci_lead_time row for a merged pull request."""
    merged_ms = round(item.merged.timestamp() * 1000)
    return {
        "event_id": f"ci-lead:{repo}:{item.number}",
        "ledger": repo,
        "ts_ms": merged_ms,
        "plan": "",
        "phase": "",
        "slice": "",
        "task": "",
        "pr": item.number,
        "branch": item.branch,
        "first_run_ms": round(item.first_run.timestamp() * 1000),
        "merged_ms": merged_ms,
        "minutes": item.minutes,
        "heads": item.heads,
        "runs": item.runs,
    }


def rows(
    repo: str,
    start: datetime,
    tiers: Mapping[str, Tally],
    merged: int,
    leads: Sequence[Lead],
    pending: Mapping[str, float],
) -> dict[metrics_outbox.Table, list[dict[str, Any]]]:
    """Outbox rows of one window: the window total, each tier, each runner pool, and each merged pull request."""
    total = sum(tiers.values(), Tally())
    cost = [
        cost_row(repo, start, (WINDOW, ALL), total, merged, sum(pending.values())),
        *(cost_row(repo, start, (TIER, name), part, merged, 0.0) for name, part in sorted(tiers.items())),
        *(cost_row(repo, start, (POOL, pool), Tally(), merged, value) for pool, value in sorted(pending.items())),
    ]
    return {COST: cost, LEAD: [lead_row(repo, item) for item in leads]}
