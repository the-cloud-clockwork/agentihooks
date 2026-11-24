"""Test units from the dependency manifest: a content fingerprint per test and file sized test batches.

A test's fingerprint covers the hashes of the units it executed, every repository file it opened, its own test
module and every test tree module that module imports, and a part every test shares: the dependency pins, the test
support modules, every Python file outside the measured trees, and the pytest invocation, which replaces the
manifest's invalidation by workflow files. A broad test, whose closure escaped the tracer or that opened files
outside the manifest, is keyed on every file under the measured trees and the test trees instead of its own units.
"""

import hashlib
import json
from collections.abc import Iterable, Mapping, Sequence
from pathlib import PurePosixPath
from typing import Any

from scripts import dep_manifest
from scripts.ci_lab.digests import TestInputs, Toolchain, test_key
from scripts.ci_lab.units import Unit
from scripts.ci_mutation import scope

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
BROAD_TREES = (*dep_manifest.MEASURED, "tests/")
KIND = "test"
BATCH_SECONDS = 60.0
UNLISTED = "unlisted"


def _digest(record: object) -> str:
    """Return the sha256 of a record's canonical JSON."""
    return hashlib.sha256(json.dumps(record, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _blob(tree: Mapping[str, bytes], path: str) -> str | None:
    """Return the sha256 of a tree file, or None when the tree lacks it."""
    content = tree.get(path)
    return hashlib.sha256(content).hexdigest() if content is not None else None


def _text(content: bytes) -> str:
    """Return a source file's text the way the manifest's change reader decodes it."""
    return content.decode(errors="replace")


def _test_module(path: str) -> bool:
    """Return whether a path is a pytest test module under the test tree."""
    return path.startswith("tests/") and path.endswith(".py") and PurePosixPath(path).name.startswith("test_")


def _shared(path: str) -> bool:
    """Return whether a Python file outside the measured trees and the test modules keys every test."""
    return path.endswith(".py") and not path.startswith(dep_manifest.MEASURED) and not _test_module(path)


def _reached(tree: Mapping[str, bytes], path: str, reached: set[str]) -> set[str]:
    """Return the test tree modules a test module reaches through its imports, added to the modules already reached.

    The manifest records no test module another test module imports, since an import's own read is not a test read.
    """
    if path in reached or path not in tree:
        return reached
    reached.add(path)
    try:
        names = scope.imported_names(_text(tree[path]), PurePosixPath(path).parent.parts)
    except SyntaxError:
        return reached
    for name in names:
        module = f"{name.replace('.', '/')}.py"
        if module.startswith("tests/"):
            _reached(tree, module, reached)
    return reached


def _unit_keys(units: Sequence[str], tree: Mapping[str, bytes]) -> list[str | None]:
    """Return the hash of every manifest unit in the tree, None for a unit the tree no longer holds."""
    pairs = [name.split("::") for name in units]
    sources = {pair[0] for pair in pairs}
    hashes = {path: dep_manifest.unit_hashes(_text(tree[path])) for path in sources if path in tree}
    return [hashes.get(path, {}).get(unit) for path, unit in pairs]


def broad(record: Mapping[str, Any]) -> bool:
    """Return whether a manifest record's dependencies are unknown, so it is keyed on the measured tree.

    A record without the tracer's escapes flag is broad; a record listing files it opened outside the manifest is too.
    """
    return record.get("escapes", True) or bool(record.get("outside"))


def fingerprints(
    manifest: Mapping[str, Any], tree: Mapping[str, bytes], invocation: Sequence[str] = PYTEST_INVOCATION
) -> dict[str, str]:
    """Return a content key per manifest test over the repository tree it runs against.

    The tree maps every tracked path to its blob content. The invocation is the interpreter and pytest arguments the
    executor runs, without its slot count. Inputs the manifest does not record, such as the clock, the network and
    environment variables, are not keyed.
    """
    shared = _digest(
        {
            "invocation": list(invocation),
            "files": {path: _blob(tree, path) for path in dep_manifest.GLOBAL_FILES.union(filter(_shared, tree))},
        }
    )
    measured = _digest({path: _blob(tree, path) for path in tree if path.startswith(BROAD_TREES)})
    names: list[str] = manifest["units"]
    units = _unit_keys(names, tree)
    owns = {dep_manifest.node(test).partition("::")[0] for test in manifest["tests"]}
    reached = {own: _reached(tree, own, set()) for own in owns}
    keys: dict[str, str] = {}
    for test, record in manifest["tests"].items():
        opened: list[str] = record["files"]
        own = dep_manifest.node(test).partition("::")[0]
        found: dict[str, object] = {
            "test": test,
            "shared": shared,
            "files": {path: _blob(tree, path) for path in {*opened, own, *reached[own]}},
        }
        if broad(record):
            found["tree"] = measured
        else:
            executed: list[int] = record["units"]
            found["units"] = {names[index]: units[index] for index in executed}
        keys[test] = _digest(found)
    return keys


def unlisted_test_hashes(manifest: Mapping[str, Any], tree: Mapping[str, bytes]) -> dict[str, str]:
    """Return the sha256 of every test module of the tree that holds no manifest test, of each conftest pytest loads
    for such a module and of each test tree module either imports, and while there is such a module, of every file
    under the measured and test trees, since the manifest records no file a test it never ran reads."""
    listed = {test.partition("::")[0] for test in manifest["tests"]}
    reached: set[str] = set()
    for path in tree:
        if _test_module(path) and path not in listed:
            for parent in PurePosixPath(path).parents:
                _reached(tree, (parent / "conftest.py").as_posix(), reached)
            _reached(tree, path, reached)
    if reached:
        reached.update(path for path in tree if path.startswith(BROAD_TREES))
    return {path: hashlib.sha256(tree[path]).hexdigest() for path in reached}


def unlisted_units(
    manifest: Mapping[str, Any], tree: Mapping[str, bytes], paths: Iterable[str], toolchain: Toolchain
) -> tuple[Unit, ...]:
    """Return one test unit per test module among the paths that the tree holds and that holds no manifest test, its
    member the module's path, keyed as a broad test over the tree and the files the manifest's `UNLISTED` notes say
    the module opened outside it, with the pytest invocation and the toolchain."""
    listed = {test.partition("::")[0] for test in manifest["tests"]}
    noted: Mapping[str, Sequence[str]] = manifest.get(UNLISTED, {})
    files = sorted({path for path in paths if _test_module(path) and path in tree and path not in listed})
    keys = fingerprints({"units": [], "tests": {path: {"files": list(noted.get(path, ()))} for path in files}}, tree)
    return tuple(
        Unit(
            KIND,
            test_key(TestInputs({path: keys[path]}, {}, PYTEST_INVOCATION, toolchain)),
            (path,),
            json.dumps({"tests": {}}),
        )
        for path in files
    )


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
    return [(test,) for test in ordered]


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


def units(
    manifest: Mapping[str, Any],
    tree: Mapping[str, bytes],
    blobs: Mapping[str, str],
    durations: Mapping[str, float],
    toolchain: Toolchain,
) -> tuple[Unit, ...]:
    """Return one test unit per batch of the manifest's tests, keyed by its tests' fingerprints over the tree, the
    blob ids of the files they opened outside the manifest, the pytest invocation and the toolchain."""
    keys = fingerprints(manifest, tree)
    found: list[Unit] = []
    for batch in batches(manifest, durations, BATCH_SECONDS):
        outside = {path for test in batch for path in manifest["tests"][test].get("outside", ())}
        opened = {path: blobs[path] for path in outside if path in blobs}
        inputs = TestInputs({test: keys[test] for test in batch}, opened, PYTEST_INVOCATION, toolchain)
        found.append(
            Unit(
                KIND, test_key(inputs), batch, json.dumps({"tests": {test: manifest["tests"][test] for test in batch}})
            )
        )
    return tuple(found)
