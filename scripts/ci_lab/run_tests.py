"""The lab's test batch executor: one pytest run of a batch in the sandbox, and one more on identical inputs after
a failure.

A pass after a failure is green and flaky, with a flake ledger row per test the first attempt failed; a second
failure is red and final, so the store keeps the negative verdict. A repository file the batch opened that its
manifest records leave out is listed on the verdict and makes it broad.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from scripts.ci_lab.sandbox import PortAllocator

__all__ = ["Attempt", "Lab", "Verdict", "flake_rows", "outside", "run"]


@dataclass(frozen=True)
class Lab:
    """Where batches run: the sandbox root, ports and environment, the dependency manifest and the flake ledger."""

    root: Path
    ports: PortAllocator
    whitelist: Mapping[str, str]
    manifest: Mapping[str, Any]
    flakes: Path
    timeout: float
    net: bool = False


@dataclass(frozen=True)
class Attempt:
    """One pytest run of a batch: its exit code, None when the timeout killed it, its output folder and its log."""

    code: int | None
    folder: Path
    log: Path


@dataclass(frozen=True)
class Verdict:
    """A batch's verdict: passed or failed, whether a retry passed it, the repository files it opened outside its
    manifest, the final attempt's durations, log and coverage data, and how many attempts ran."""

    outcome: str
    flaky: bool
    outside: tuple[str, ...]
    durations: Mapping[str, float]
    attempts: int
    log: Path
    coverage: Path

    @property
    def broad(self) -> bool:
        """Whether the batch opened a repository file its manifest records leave out."""
        return bool(self.outside)


def outside(opened: set[str], batch: Sequence[str], tree: Path, manifest: Mapping[str, Any]) -> tuple[str, ...]:
    """Return the tree files the batch opened that neither its manifest records nor the inputs every test shares hold."""
    raise NotImplementedError


def flake_rows(first: Attempt, batch: Sequence[str]) -> list[dict[str, Any]]:
    """Return the flake ledger rows of a batch whose retry passed: one per test the first attempt failed, or one per
    batch test when the first attempt recorded no failed test."""
    raise NotImplementedError


def run(batch: Sequence[str], tree: Path, venv: Path, slots: int, lab: Lab) -> Verdict:
    """Run a batch from tree with venv's python on slots workers, once more on identical inputs after a failure."""
    raise NotImplementedError
