"""Lab mutation units: one stats unit per test file and one mutant unit per changed function.

A stats unit carries the stats cache identity of its test file. A mutant unit names every mutant
of one changed function and is keyed only by what decides their verdicts, so line numbers, the
changed line scope and clearances apply at grade time.
"""

from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path

from scripts.ci_lab.units import Unit


def stats_units(root: Path, files: Sequence[str], options: Sequence[str] = ()) -> tuple[Unit, ...]:
    """Key one stats unit per test file with the identity the stats cache stores for its unmeasured record."""
    raise NotImplementedError


def mutant_units(
    root: Path, changes: Mapping[str, Iterable[int]], covering: Mapping[str, Mapping[str, str]]
) -> tuple[Unit, ...]:
    """Name every mutant of each changed function and key it by the inputs that decide its verdicts."""
    raise NotImplementedError
