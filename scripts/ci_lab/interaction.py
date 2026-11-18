"""Merge group interaction rule of the lab: the test units a merge group head must run, or the whole suite."""

from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final, Literal

__all__ = ["FULL", "Group", "decide", "entry_limit", "full_reason", "invalidated", "misses_most", "overlap", "touched"]

Change = tuple[str, str | None, str | None]
FULL: Final = "FULL"


@dataclass(frozen=True)
class Group:
    """A merge group head: its diff against dev, each entry's changes from dev to its graded tree, the pyproject flag."""

    changes: tuple[Change, ...]
    entries: tuple[tuple[Change, ...], ...]
    pyproject: bool


def touched(entry: Sequence[Change]) -> frozenset[str]:
    """Return the Python modules an entry's changes touch."""
    raise NotImplementedError


def overlap(entries: Sequence[Sequence[Change]]) -> bool:
    """Return whether two entries touch a common module."""
    raise NotImplementedError


def invalidated(manifest: dict[str, Any], entries: Sequence[Sequence[Change]]) -> frozenset[str]:
    """Return the union of the manifest test units each entry's changes invalidate."""
    raise NotImplementedError


def misses_most(manifest: Mapping[str, Any], cached: Collection[str]) -> bool:
    """Return whether more than half the manifest's test units have no cached verdict."""
    raise NotImplementedError


def entry_limit(lab: Mapping[str, Any]) -> int:
    """Return lab.queue.full_suite_entries of a validated lab block."""
    raise NotImplementedError


def full_reason(group: Group, manifest: dict[str, Any], cached: Collection[str], limit: int) -> str | None:
    """Return the clause that sends the group to the whole suite, or None."""
    raise NotImplementedError


def decide(
    group: Group, manifest: dict[str, Any], cached: Collection[str], limit: int
) -> frozenset[str] | Literal["FULL"]:
    """Return the test units the merge group head must run, or FULL."""
    raise NotImplementedError
