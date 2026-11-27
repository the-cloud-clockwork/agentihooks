"""The lab reuse audit: regrades that check the verdict store's reuse against fresh grades of the same units.

promotion_audit grades a dev head's whole suite with no store reuse and compares every verdict with the cached one;
sample regrades a deterministic share of a day's store hits in the backfill class. A mismatch disables reuse of its
kind, files one ci task per kind on the ledger naming each cached run and the audit run, and raises
CILabVerdictMismatch. A kind's mismatches stay recorded as unfiled in the store until the ledger takes its task,
and the next audit files them again. The audit never enables a kind again; that takes a pull request.
"""

import json
import math
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from typing import Any, Protocol

from scripts.ci_lab.digests import digest
from scripts.ci_lab.store import Store, Verdict
from scripts.ci_lab.units import Priority

__all__ = [
    "NOT_DUPLICATE",
    "SENDER",
    "SHARE",
    "TASK_PREFIX",
    "CILabVerdictMismatch",
    "Cached",
    "Lab",
    "Mismatch",
    "Ports",
    "Regrade",
    "Tasks",
    "chosen",
    "mismatches",
    "promotion_audit",
    "sample",
    "unfiled",
]

SHARE = 0.05
SENDER = "swarm"
TASK_PREFIX = "lab-mismatch-"
NOT_DUPLICATE = "The lab audit files one task per kind and audit run whose regrade differs from the cached verdicts."


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
        kinds = sorted({mismatch.cached.kind for mismatch in self.mismatches})
        super().__init__(f"regraded verdicts differ from the cached ones for {', '.join(kinds)}")


class Lab(Protocol):
    """The grading an audit drives."""

    def suite(self, head: str) -> Sequence[Cached]:
        """Return every unit of the head's cached promotion grade."""
        ...

    def hits(self, day: str) -> Sequence[Cached]:
        """Return every unit the store answered on a UTC day, once per unit and head."""
        ...

    def grade(self, units: Sequence[Cached], tier: Priority) -> Regrade:
        """Grade the units in the tier's class with no store reuse; return the run and its verdicts."""
        ...


class Tasks(Protocol):
    """The ledger task API an audit files through: `LedgerClient.add_task`."""

    def add_task(self, slug: str, fields: dict[str, Any], by: str) -> object:
        """Add one task to the ledger."""
        ...


@dataclass(frozen=True)
class Ports:
    """What an audit reads and writes: the verdict store, the lab, the ledger task API and the ledger it files on."""

    store: Store
    lab: Lab
    tasks: Tasks
    slug: str


def chosen(day: str, hits: Sequence[Cached], share: float) -> list[Cached]:
    """Return the ceiling of the share of a day's hits, taken in the order of each hit's digest under the day digest."""
    seed = digest({"day": day})
    ranked = sorted(hits, key=lambda hit: digest({"seed": seed, "unit": f"{hit.kind}:{hit.key}", "head": hit.head}))
    return ranked[: math.ceil(share * len(ranked))]


def mismatches(store: Store, units: Sequence[Cached], regrade: Regrade) -> list[Mismatch]:
    """Return every unit whose regrade differs from its cached verdict; a cached flaky pass agrees with a failure."""
    graded = {(verdict.kind, verdict.key): verdict for verdict in regrade.verdicts}
    found: list[Mismatch] = []
    for unit in units:
        cached = store.get(unit.kind, unit.key)
        fresh = graded.get((unit.kind, unit.key))
        if cached is not None and fresh is not None and cached.passed != fresh.passed and not cached.flaky:
            found.append(Mismatch(cached, fresh, unit.run, regrade.run))
    return found


def unfiled(store: Store) -> list[Mismatch]:
    """Return the mismatches of every kind whose task an earlier audit could not file, by kind."""
    return [
        Mismatch(Verdict(**row["cached"]), Verdict(**row["regraded"]), row["cached_run"], row["audit_run"])
        for _, record in sorted(store.unfiled().items())
        for row in json.loads(record)
    ]


def _word(verdict: Verdict) -> str:
    return "passed" if verdict.passed else "failed"


def _line(mismatch: Mismatch) -> str:
    cached, regraded = mismatch.cached, mismatch.regraded
    return (
        f"{cached.key} cached {_word(cached)} by run {mismatch.cached_run}, "
        f"regraded {_word(regraded)} by run {mismatch.audit_run}"
    )


def _description(kind: str, found: Sequence[Mismatch]) -> str:
    return (
        f"The lab audit regraded {kind} units differently from their cached verdicts: "
        + "; ".join(_line(mismatch) for mismatch in found)
        + f". Reuse of {kind} verdicts is off until a pull request enables the kind again "
        + "with the fix for the input its key misses."
    )


def _file(ports: Ports, kind: str, found: Sequence[Mismatch], run: str) -> None:
    """File one kind's mismatches as a ci task naming every cached run and the audit run."""
    fields = {
        "task": f"{TASK_PREFIX}{kind}-{digest({'kind': kind, 'run': run})[:16]}",
        "title": f"Lab verdict mismatch: {kind} reuse disabled",
        "lane": "ci",
        "kind": "ci",
        "rank": "urgent",
        "description": _description(kind, found),
        "not_duplicate": NOT_DUPLICATE,
    }
    ports.tasks.add_task(ports.slug, fields, SENDER)


def _audit(ports: Ports, units: Sequence[Cached], tier: Priority) -> Regrade:
    """Grade the units uncached; with the unfiled mismatches, disable and record each differing kind, file it, raise."""
    regrade = ports.lab.grade(units, tier)
    found = [*unfiled(ports.store), *mismatches(ports.store, units, regrade)]
    kinds = {kind: [m for m in found if m.cached.kind == kind] for kind in sorted({m.cached.kind for m in found})}
    for kind, kept in kinds.items():
        ports.store.disable(kind)
        ports.store.keep_unfiled(kind, json.dumps([asdict(mismatch) for mismatch in kept]))
    for kind, kept in kinds.items():
        _file(ports, kind, kept, regrade.run)
        ports.store.filed(kind)
    if found:
        raise CILabVerdictMismatch(found)
    return regrade


def promotion_audit(ports: Ports, head: str) -> Regrade:
    """Grade the head's whole suite uncached in the promotion class and compare every verdict with the cached one."""
    return _audit(ports, ports.lab.suite(head), Priority.PROMOTION)


def sample(ports: Ports, day: str, share: float = SHARE) -> Regrade:
    """Regrade a deterministic share of the day's store hits in the backfill class and compare them."""
    return _audit(ports, chosen(day, ports.lab.hits(day), share), Priority.BACKFILL)
