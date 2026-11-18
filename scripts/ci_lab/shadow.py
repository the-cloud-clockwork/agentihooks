"""Shadow comparison of lab verdicts with GitHub Actions: one row per gate and head."""

import time
from collections.abc import Callable, Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from scripts.ci_lab import metrics
from scripts.ci_lab.objects import Objects

__all__ = [
    "MUTATION",
    "TESTS",
    "UNMAPPED",
    "Row",
    "Shadow",
    "ancestor",
    "gate_of",
    "grader_revision",
    "main",
    "report",
    "rows",
    "static_jobs",
]

TESTS = "tests"
MUTATION = "mutation"
UNMAPPED = "unmapped"


@dataclass(frozen=True)
class Row:
    """One gate of one head with both verdicts, the grader revision the lab graded it with and whether they differ."""

    head: str
    gate: str
    grader: str
    lab_verdict: str
    actions_verdict: str
    mismatch: bool


def gate_of(name: str, static: Collection[str]) -> str:
    """Return the gate a check run name maps to."""
    return UNMAPPED


def grader_revision(summary: str | None) -> str:
    """Return the grader revision a lab check run summary names."""
    return ""


def static_jobs(root: Path) -> frozenset[str]:
    """Return the gate id of every static job of the workflow tree at root."""
    return frozenset()


def _now_ms() -> int:
    return time.time_ns()


class Shadow:
    """The shadow recorder: folds check_run.completed deliveries into one row per gate and head."""

    def __init__(
        self,
        client: Any,
        objects: Objects,
        sink: metrics.Sink,
        registry: metrics.Registry,
        static: Collection[str],
        now_ms: Callable[[], int] = _now_ms,
    ) -> None:
        self.client = client
        self.objects = objects
        self.sink = sink
        self.registry = registry
        self.static = static
        self.now_ms = now_ms

    def observe(self, payload: Mapping[str, Any]) -> Row | None:
        """Fold one check run delivery into its gate and head."""
        return None

    def record(self, head: str, gate: str, lab_verdict: str, actions_verdict: str, grader: str) -> Row:
        """Write one gate and head's row."""
        return Row(head, gate, grader, lab_verdict, actions_verdict, False)


def rows(client: Any) -> list[Row]:
    """Return every shadow row in Redis that holds both verdicts."""
    return []


def report(found: Iterable[Row], unmapped: Iterable[str], since: str, descends: Callable[[str, str], bool]) -> str:
    """Return heads compared and mismatches per gate since a grader revision."""
    return ""


def ancestor(repo: Path, older: str, newer: str) -> bool:
    """Whether older is newer or one of its ancestors in the git repository at repo."""
    return False


def main(argv: Sequence[str], environ: Mapping[str, str]) -> int:
    """Print the shadow report."""
    return 0
