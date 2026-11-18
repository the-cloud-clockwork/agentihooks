"""The lab reuse audit: regrades that check the verdict store's reuse against fresh grades of the same units.

Audit.promotion_audit grades a dev head's whole suite with no store reuse and compares every verdict with the cached
one; Audit.sample regrades a deterministic share of a day's store hits in the backfill class. A mismatch disables
reuse of its kind, files one ci task per kind on the ledger naming the cached run and the audit run, and raises
CILabVerdictMismatch. Only a pull request enables a kind again.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from scripts.ci_lab.store import Store, Verdict
from scripts.ci_lab.units import Priority

__all__ = [
    "NOT_DUPLICATE",
    "SHARE",
    "Audit",
    "CILabVerdictMismatch",
    "Cached",
    "Lab",
    "Mismatch",
    "Regrade",
    "Tasks",
    "chosen",
    "mismatches",
]

SHARE = 0.05
NOT_DUPLICATE = ""


@dataclass(frozen=True)
class Cached:
    """A unit a grade answered: its kind and key, the head it graded and the run whose verdict the grade used."""

    kind: str
    key: str
    head: str
    run: str


@dataclass(frozen=True)
class Regrade:
    """An audit's grade: its run and the verdict of every unit it graded."""

    run: str
    verdicts: tuple[Verdict, ...]


@dataclass(frozen=True)
class Mismatch:
    """A unit whose regrade differs from its cached verdict, with the run of each."""

    cached: Verdict
    regraded: Verdict
    cached_run: str
    audit_run: str


class CILabVerdictMismatch(RuntimeError):
    """An audit found regraded verdicts that differ from the cached ones; reuse of their kinds is off."""

    def __init__(self, found: Sequence[Mismatch]) -> None:
        self.mismatches = tuple(found)
        super().__init__("")


class Lab(Protocol):
    """The grading an audit drives."""

    def suite(self, head: str) -> Sequence[Cached]:
        """Return every unit of the head's cached promotion grade."""
        ...

    def hits(self, day: str) -> Sequence[Cached]:
        """Return every unit the store answered on a UTC day."""
        ...

    def grade(self, units: Sequence[Cached], tier: Priority) -> Regrade:
        """Grade the units in the tier's class with no store reuse; return the run and its verdicts."""
        ...


class Tasks(Protocol):
    """The ledger task API an audit files through: `LedgerClient.add_task`."""

    def add_task(self, slug: str, fields: dict[str, Any], by: str) -> object:
        """Add one task to the ledger."""
        ...


def chosen(day: str, hits: Sequence[Cached], share: float) -> list[Cached]:
    """Return the share of a day's hits that ranks first by digest under the day digest."""
    return list(hits)


def mismatches(store: Store, units: Sequence[Cached], regrade: Regrade) -> list[Mismatch]:
    """Return every unit whose regraded verdict differs from its cached one."""
    return []


class Audit:
    """Regrades that check reuse: each compares a fresh grade with the store and stops reuse of a differing kind."""

    def __init__(self, store: Store, lab: Lab, tasks: Tasks, slug: str) -> None:
        self.store = store
        self.lab = lab
        self.tasks = tasks
        self.slug = slug

    def promotion_audit(self, head: str) -> Regrade:
        """Grade the head's whole suite uncached in the promotion class and compare every verdict with the cached one."""
        return Regrade("", ())

    def sample(self, day: str, share: float = SHARE) -> Regrade:
        """Regrade a deterministic share of the day's store hits in the backfill class and compare them."""
        return Regrade("", ())
