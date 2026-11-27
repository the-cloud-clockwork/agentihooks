"""Cache lookups the lab counts: the verdict store the scheduler asks, and the git revisions and venvs a worker unit
fetched or built, which the unit's report carries under its caches key."""

from collections.abc import Mapping, Sequence
from typing import Any

__all__ = ["GIT", "REPORTED", "UV", "VERDICT", "read", "report"]

VERDICT = "verdict"
GIT = "git"
UV = "uv"
REPORTED = (GIT, UV)
HITS = "hits"
MISSES = "misses"
KEY = "caches"


def _counts(fetched: Sequence[bool]) -> dict[str, int]:
    """Count each lookup flagged False as a hit and each flagged True as a miss."""
    misses = sum(fetched)
    return {HITS: len(fetched) - misses, MISSES: misses}


def report(git: Sequence[bool], uv: Sequence[bool]) -> dict[str, dict[str, int]]:
    """Return a unit report's cache lookups: per revision whether the store fetched it, per venv whether it was built."""
    return {GIT: _counts(git), UV: _counts(uv)}


def read(unit: Mapping[str, Any]) -> dict[str, tuple[int, int]]:
    """Return the hits and misses of each worker cache a unit report carries; an older worker's report carries none."""
    carried: Mapping[str, Mapping[str, int]] = unit.get(KEY, {})
    return {name: (int(carried[name][HITS]), int(carried[name][MISSES])) for name in REPORTED if name in carried}
