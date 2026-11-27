"""Tests for the lab merge group interaction rule: the units a merge group head runs, or the whole suite."""

from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Any

import pytest
import yaml
from hypothesis import given, settings
from hypothesis import strategies as st

from scripts import ci_select, dep_manifest
from scripts.ci_lab import delivery, interaction

Change = tuple[str, str | None, str | None]

PROPERTY = settings(database=None, deadline=None, derandomize=True, max_examples=40)
MODULES = tuple(f"scripts/m{number}.py" for number in range(8))
LIMIT = len(MODULES)
LAB: dict[str, object] = {
    "authoritative": [],
    "mutation": {"run_on": "every_push", "mutants_per_changed_line": "all", "arid": []},
    "check_prefix": "lab",
}
HARNESS_PATHS = (*sorted(ci_select.HARNESS), "conftest.py", "tests/conftest.py")


def edit(path: str) -> Change:
    """Return a change that edits the body of the module's one function."""
    return (path, "def f():\n    return 1\n", "def f():\n    return 2\n")


def covering(path: str) -> str:
    """Return the id of the one test the fixture manifest records as covering the module's function."""
    return f"tests/test_{Path(path).stem}.py::test_f"


MANIFEST: dict[str, Any] = {
    "version": 2,
    "commit": "c",
    "environment": "e",
    "units": [f"{path}::f" for path in MODULES],
    "tests": {
        covering(path): {"units": [number], "files": [], "escapes": False} for number, path in enumerate(MODULES)
    },
}
CACHED = frozenset(MANIFEST["tests"])


def group(
    entries: Sequence[Sequence[Change]], extra: Iterable[Change] = (), pyproject: bool = False
) -> interaction.Group:
    """Return a merge group whose diff against dev is its entries' changes plus the extra changes."""
    changes = (*(change for entry in entries for change in entry), *extra)
    return interaction.Group(changes=changes, entries=tuple(tuple(entry) for entry in entries), pyproject=pyproject)


@st.composite
def disjoint(draw: st.DrawFn) -> list[list[Change]]:
    """Draw one to four entries, each editing its own modules, with no module in two entries."""
    owners = draw(st.lists(st.integers(-1, 3), min_size=len(MODULES), max_size=len(MODULES)))
    entries = [
        [edit(path) for path, owner in zip(MODULES, owners, strict=True) if owner == index] for index in range(4)
    ]
    return [entry for entry in entries if entry] or [[edit(MODULES[0])]]


def modules(entries: Sequence[Sequence[Change]]) -> set[str]:
    """Return every path the entries change."""
    return {path for entry in entries for path, _, _ in entry}


@PROPERTY
@given(entries=disjoint())
def test_a_disjoint_group_returns_the_union_of_its_entries_invalidated_units(entries: list[list[Change]]) -> None:
    union = set[str]().union(*(dep_manifest.invalidated(MANIFEST, entry)[0] for entry in entries))
    assert interaction.full_reason(group(entries), MANIFEST, CACHED, LIMIT) is None
    assert interaction.decide(group(entries), MANIFEST, CACHED, LIMIT) == frozenset(union)
    assert union == {covering(path) for path in modules(entries)}


@PROPERTY
@given(entries=disjoint(), path=st.sampled_from(HARNESS_PATHS))
def test_a_harness_change_in_the_group_diff_runs_the_whole_suite(entries: list[list[Change]], path: str) -> None:
    head = group(entries, extra=[(path, "a = 1\n", "a = 2\n")])
    assert interaction.full_reason(head, MANIFEST, CACHED, LIMIT) == "harness"
    assert interaction.decide(head, MANIFEST, CACHED, LIMIT) == "FULL"


@pytest.mark.parametrize(
    ("path", "pyproject", "expected"),
    [
        ("pyproject.toml", True, "harness"),
        ("pyproject.toml", False, None),
        ("tests/ci_lab/conftest.py", False, None),
        ("tests/conftest.py", False, "harness"),
        ("docs/reference/delivery-tiers.md", True, None),
    ],
)
def test_the_harness_clause_follows_the_narrowed_selection(path: str, pyproject: bool, expected: str | None) -> None:
    head = group([[edit(MODULES[0])]], extra=[(path, "a = 1\n", "a = 2\n")], pyproject=pyproject)
    assert interaction.full_reason(head, MANIFEST, CACHED, LIMIT) == expected


@PROPERTY
@given(count=st.integers(1, len(MODULES)), limit=st.integers(1, len(MODULES)))
def test_more_entries_than_the_limit_run_the_whole_suite(count: int, limit: int) -> None:
    head = group([[edit(path)] for path in MODULES[:count]])
    expected = "entries" if count > limit else None
    assert interaction.full_reason(head, MANIFEST, CACHED, limit) == expected
    assert (interaction.decide(head, MANIFEST, CACHED, limit) == "FULL") is (count > limit)


@pytest.mark.parametrize(("count", "expected"), [(3, None), (4, "entries")])
def test_a_group_of_exactly_the_limit_keeps_its_units(count: int, expected: str | None) -> None:
    head = group([[edit(path)] for path in MODULES[:count]])
    assert interaction.full_reason(head, MANIFEST, CACHED, 3) == expected


@PROPERTY
@given(entries=disjoint(), shared=st.sampled_from(MODULES))
def test_two_entries_touching_one_module_run_the_whole_suite(entries: list[list[Change]], shared: str) -> None:
    head = group([*entries, [edit(shared)]])
    expected = "overlap" if shared in modules(entries) else None
    assert interaction.full_reason(head, MANIFEST, CACHED, LIMIT) == expected
    assert (interaction.decide(head, MANIFEST, CACHED, LIMIT) == "FULL") is (expected is not None)


def test_the_first_and_last_entries_sharing_a_module_overlap() -> None:
    entries = [[edit(MODULES[0])], [edit(MODULES[1])], [edit(MODULES[2]), edit(MODULES[0])]]
    assert interaction.overlap(entries) is True
    assert interaction.overlap(entries[:2]) is False


@pytest.mark.parametrize(
    ("entries", "extra", "cached", "expected"),
    [
        ([[edit(path)] for path in MODULES[:4]], [("tests/shards.py", "a", "b")], CACHED, "harness"),
        ([[edit(MODULES[0])], [edit(MODULES[0])], [edit(MODULES[1])], [edit(MODULES[2])]], [], CACHED, "entries"),
        ([[edit(MODULES[0])], [edit(MODULES[0])]], [], frozenset[str](), "overlap"),
    ],
)
def test_the_clauses_hold_in_their_documented_order(
    entries: list[list[Change]], extra: list[Change], cached: frozenset[str], expected: str
) -> None:
    assert interaction.full_reason(group(entries, extra=extra), MANIFEST, cached, 3) == expected


@pytest.mark.parametrize("path", ["feature-map.json", "docs/reference/delivery-tiers.md", "tests/TYPE_BASELINE.json"])
def test_entries_sharing_only_a_file_that_is_no_module_do_not_overlap(path: str) -> None:
    entries = [[edit(MODULES[0]), (path, "a", "b")], [edit(MODULES[1]), (path, "a", "c")]]
    assert interaction.touched(entries[0]) == frozenset({MODULES[0]})
    assert interaction.full_reason(group(entries), MANIFEST, CACHED, LIMIT) is None


@PROPERTY
@given(missing=st.sets(st.sampled_from(sorted(CACHED))))
def test_a_cache_missing_more_than_half_the_units_runs_the_whole_suite(missing: set[str]) -> None:
    head = group([[edit(MODULES[0])]])
    expected = "cache" if 2 * len(missing) > len(CACHED) else None
    assert interaction.full_reason(head, MANIFEST, CACHED - missing, LIMIT) == expected
    assert (interaction.decide(head, MANIFEST, CACHED - missing, LIMIT) == "FULL") is (expected is not None)


@pytest.mark.parametrize(("missing", "expected"), [(3, False), (4, False), (5, True), (8, True)])
def test_the_cache_clause_holds_only_past_half(missing: int, expected: bool) -> None:
    cached = sorted(CACHED)[missing:]
    assert interaction.misses_most(MANIFEST, cached) is expected


def test_an_empty_manifest_never_misses_most() -> None:
    assert interaction.misses_most({**MANIFEST, "units": [], "tests": {}}, []) is False


def tree(root: Path, lab: dict[str, object]) -> Path:
    """Write a repository tree holding an empty workflow folder and a delivery.yaml with the given lab block."""
    (root / ".github/workflows").mkdir(parents=True)
    (root / "delivery.yaml").write_text(yaml.safe_dump({"lab": lab}))
    return root


def test_the_entry_limit_defaults_to_the_largest_merge_group(tmp_path: Path) -> None:
    assert interaction.entry_limit(delivery.lab(tree(tmp_path, LAB))) == 5


@pytest.mark.parametrize("entries", [1, 3, 12])
def test_the_entry_limit_reads_lab_queue_full_suite_entries(tmp_path: Path, entries: int) -> None:
    lab = delivery.lab(tree(tmp_path, {**LAB, "queue": {"full_suite_entries": entries}}))
    assert interaction.entry_limit(lab) == entries


def test_an_empty_lab_queue_keeps_the_default_limit(tmp_path: Path) -> None:
    assert interaction.entry_limit(delivery.lab(tree(tmp_path, {**LAB, "queue": {}}))) == 5


@pytest.mark.parametrize(
    "queue",
    [
        {"full_suite_entries": 0},
        {"full_suite_entries": -1},
        {"full_suite_entries": 1.5},
        {"full_suite_entries": "five"},
        {"full_suite_entries": True},
        {"extra": 1},
        [],
    ],
)
def test_invalid_lab_queue_is_an_error(tmp_path: Path, queue: object) -> None:
    with pytest.raises(delivery.DeliveryError, match="queue"):
        delivery.lab(tree(tmp_path, {**LAB, "queue": queue}))
