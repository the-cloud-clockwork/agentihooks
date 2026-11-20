"""Merge group interaction rule of the lab: the test units a merge group head must run, or the whole suite."""

from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final, Literal, cast

from scripts import ci_select, dep_manifest

__all__ = [
    "FULL",
    "FULL_SUITE_ENTRIES",
    "Group",
    "decide",
    "entry_limit",
    "full_reason",
    "invalidated",
    "misses_most",
    "overlap",
    "touched",
]

Change = tuple[str, str | None, str | None]
FULL: Final = "FULL"
FULL_SUITE_ENTRIES = 5
MODULE_SUFFIX = ".py"
HARNESS = "harness"
ENTRIES = "entries"
OVERLAP = "overlap"
CACHE = "cache"
INVALIDATED = cast("Callable[[dict[str, Any], list[Change]], tuple[set[str], str | None]]", dep_manifest.invalidated)


@dataclass(frozen=True)
class Group:
    """A merge group head.

    changes is the group head's diff against dev and each entry holds the changes of one first parent commit
    between dev and the head against its own first parent, both as (path, old text, new text). pyproject says
    whether a pyproject.toml change reaches every test, as scripts.ci_select.pyproject_reaches_tests decides.
    """

    changes: tuple[Change, ...]
    entries: tuple[tuple[Change, ...], ...]
    pyproject: bool


def touched(entry: Sequence[Change]) -> frozenset[str]:
    """Return the Python modules an entry's changes touch."""
    return frozenset(path for path, _, _ in entry if path.endswith(MODULE_SUFFIX))


def overlap(entries: Sequence[Sequence[Change]]) -> bool:
    """Return whether two entries touch a common module: the entries' module counts exceed their union."""
    modules = [touched(entry) for entry in entries]
    return sum(map(len, modules)) > len(frozenset[str]().union(*modules))


def invalidated(manifest: dict[str, Any], entries: Sequence[Sequence[Change]]) -> frozenset[str]:
    """Return the union of the manifest test units each entry's changes invalidate."""
    return frozenset[str]().union(*(INVALIDATED(manifest, list(entry))[0] for entry in entries))


def misses_most(manifest: Mapping[str, Any], cached: Collection[str]) -> bool:
    """Return whether more than half the manifest's test units have no cached verdict."""
    units = set[str](manifest["tests"])
    return 2 * len(units.difference(cached)) > len(units)


def entry_limit(lab: Mapping[str, Any]) -> int:
    """Return lab.queue.full_suite_entries of a validated lab block, FULL_SUITE_ENTRIES when it is left out."""
    queue: Mapping[str, int] = lab.get("queue", {})
    return queue.get("full_suite_entries", FULL_SUITE_ENTRIES)


def full_reason(group: Group, manifest: dict[str, Any], cached: Collection[str], limit: int) -> str | None:
    """Return the clause that sends the group to the whole suite, or None when its entries' units suffice."""
    if ci_select.needs_full_suite([path for path, _, _ in group.changes], group.pyproject):
        return HARNESS
    if len(group.entries) > limit:
        return ENTRIES
    if overlap(group.entries):
        return OVERLAP
    if misses_most(manifest, cached):
        return CACHE
    return None


def decide(
    group: Group, manifest: dict[str, Any], cached: Collection[str], limit: int
) -> frozenset[str] | Literal["FULL"]:
    """Return the union of the entries' invalidated test units, or FULL when a clause of full_reason holds."""
    if full_reason(group, manifest, cached, limit) is not None:
        return FULL
    return invalidated(manifest, group.entries)
