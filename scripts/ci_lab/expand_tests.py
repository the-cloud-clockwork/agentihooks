"""Test units from the dependency manifest: a content fingerprint per test and file sized test batches.

A test's fingerprint covers the hashes of the units it executed, every repository file it opened, its own test
module, and a part every test shares: the dependency pins, the test support modules, every Python file outside the
measured trees, and the pytest invocation, which replaces the manifest's invalidation by workflow files. A broad
test, whose closure escaped the tracer or that opened files outside the manifest, is keyed on the whole measured
source tree instead of its own units and files.
"""

import hashlib
import json
from collections.abc import Mapping, Sequence
from pathlib import PurePosixPath
from typing import Any

from tests import dep_manifest

PYTEST_INVOCATION = (
    "pytest",
    "--dist",
    "loadgroup",
    "--timeout=60",
    "-p",
    "tests.pass_record",
    "-p",
    "tests.flake_ledger",
)
BROAD_SIGNALS = ("escapes", "outside")
BROAD_TREES = (*dep_manifest.MEASURED, "tests/")


def _digest(record: object) -> str:
    """Return the sha256 of a record's canonical JSON."""
    return hashlib.sha256(json.dumps(record, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _blob(tree: Mapping[str, bytes], path: str) -> str | None:
    """Return the sha256 of a tree file, or None when the tree lacks it."""
    content = tree.get(path)
    return hashlib.sha256(content).hexdigest() if content is not None else None


def _shared(path: str) -> bool:
    """Return whether a Python file outside the measured trees and the test modules keys every test."""
    if not path.endswith(".py") or path.startswith(dep_manifest.MEASURED):
        return False
    return not path.startswith("tests/") or not PurePosixPath(path).name.startswith("test_")


def _unit_keys(units: Sequence[str], tree: Mapping[str, bytes]) -> list[str | None]:
    """Return the hash of every manifest unit in the tree, None for a unit the tree no longer holds."""
    sources = {name.partition("::")[0] for name in units}
    hashes = {path: dep_manifest.unit_hashes(tree[path].decode("utf-8", "replace")) for path in sources if path in tree}
    return [hashes.get(path, {}).get(unit) for path, _, unit in (name.partition("::") for name in units)]


def broad(record: Mapping[str, Any]) -> bool:
    """Return whether a manifest record's dependencies are unknown, so it is keyed on the measured tree."""
    return any(record.get(signal) for signal in BROAD_SIGNALS)


def fingerprints(
    manifest: Mapping[str, Any], tree: Mapping[str, bytes], invocation: Sequence[str] = PYTEST_INVOCATION
) -> dict[str, str]:
    """Return a content key per manifest test over the repository tree it runs against."""
    shared = _digest(
        {
            "invocation": list(invocation),
            "files": {path: _blob(tree, path) for path in dep_manifest.GLOBAL_FILES.union(filter(_shared, tree))},
        }
    )
    measured = _digest({path: _blob(tree, path) for path in tree if path.startswith(BROAD_TREES)})
    names: list[str] = manifest["units"]
    units = _unit_keys(names, tree)
    keys: dict[str, str] = {}
    for test, record in manifest["tests"].items():
        if broad(record):
            keys[test] = _digest({"test": test, "shared": shared, "tree": measured})
            continue
        executed: list[int] = record["units"]
        opened: list[str] = record["files"]
        own = dep_manifest.node(test).partition("::")[0]
        keys[test] = _digest(
            {
                "test": test,
                "shared": shared,
                "units": {names[index]: units[index] for index in executed},
                "files": {path: _blob(tree, path) for path in {*opened, own}},
            }
        )
    return keys


def _parts(tests: list[str], seconds: Mapping[str, float], target: float) -> list[tuple[str, ...]]:
    """Return the fewest longest first balanced parts of one file's tests that each fit the target."""
    ordered = sorted(tests, key=lambda test: -seconds[test])
    for count in range(2, len(tests)):
        parts = [[test] for test in ordered[:count]]
        loads = [seconds[test] for test in ordered[:count]]
        for test in ordered[count:]:
            lightest = loads.index(min(loads))
            parts[lightest].append(test)
            loads[lightest] += seconds[test]
        if max(loads) <= target:
            return [tuple(part) for part in parts]
    return [(test,) for test in tests]


def batches(manifest: Mapping[str, Any], durations: Mapping[str, float], target: float) -> list[tuple[str, ...]]:
    """Return the manifest tests cut into batches of one test file each, split to fit the target seconds.

    A test without a recorded duration counts zero seconds; a file whose tests carry an xdist group stays whole.
    """
    files: dict[str, list[str]] = {}
    for test in sorted(manifest["tests"]):
        files.setdefault(dep_manifest.node(test).partition("::")[0], []).append(test)
    found: list[tuple[str, ...]] = []
    for tests in files.values():
        seconds = {test: durations.get(dep_manifest.node(test), 0.0) for test in tests}
        if any(dep_manifest.node(test) != test for test in tests) or sum(seconds.values()) <= target:
            found.append(tuple(tests))
        else:
            found.extend(_parts(tests, seconds, target))
    return found
