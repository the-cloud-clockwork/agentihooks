"""Lab mutation units: one stats unit per test file and one mutant unit per changed function.

A stats unit carries the stats cache identity of its test file. A mutant unit names every mutant
of one changed function and is keyed only by what decides their verdicts, so line numbers, the
changed line scope and clearances apply at grade time. A function whose mutants price over the
sandbox timeout gets several mutant units, each with its own run of them.
"""

import ast
import hashlib
import math
import re
import subprocess
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any

import libcst as cst
from libcst.metadata import ParentNodeProvider, PositionProvider, WhitespaceInclusivePositionProvider
from mutmut.mutation.file_mutation import group_by_top_level_node
from mutmut.mutation.trampoline_templates import mangle_function_name
from mutmut.utils.format_utils import get_mutant_name

from scripts.ci_lab import digests
from scripts.ci_lab.units import Unit
from scripts.ci_mutation.plan import MUTANT_SECONDS
from scripts.ci_mutation.scope import module_name
from scripts.ci_mutation.selection import created_mutations
from scripts.ci_mutation.stats_cache import (
    CONFIGURATION,
    CONTEXT_FIELDS,
    SCHEMA,
    TRACKED,
    UNKEYED,
    StatsInputs,
    runtime_digest,
)
from scripts.ci_mutation.stats_dependencies import StoredStats, digest  # pyright: ignore[reportUnknownVariableType]

__all__ = ["covering", "mutant_units", "stats_units"]

RUNNER = "scripts/ci_mutation/"
PIN = re.compile(r"""["']mutmut\s*==\s*([^"'\s]+)\s*["']""")
DEFINITIONS = (ast.FunctionDef, ast.AsyncFunctionDef)


def tracked(root: Path) -> list[str]:
    """List the files git tracks under root."""
    return [path for path in subprocess.check_output(TRACKED, cwd=root, text=True).split("\0") if path]


def file_digest(path: Path) -> str:
    """Return the sha256 of a file's bytes."""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def resources_of(paths: list[str]) -> dict[str, set[str]]:
    """Index each file under every suffix of its path and every parent folder, as the stats cache does."""
    resources: dict[str, set[str]] = {}
    for path in paths:
        parts = Path(path).parts
        for index in range(len(parts)):
            resources.setdefault("/".join(parts[index:]), set()).add(path)
        for parent in Path(path).parents:
            resources.setdefault(str(parent) + "/", set()).add(path)
    return resources


def stats_units(root: Path, files: Sequence[str], options: Sequence[str] = ()) -> tuple[Unit, ...]:
    """Key one stats unit per test file by the stats cache identity, at its current SCHEMA, of its static closure.

    The key takes no run output, so it differs from the measured records a stats run stores for the same file.
    """
    paths = tracked(root)
    present = [path for path in paths if (root / path).is_file()]
    sources = {path: file_digest(root / path) for path in present if path in CONFIGURATION or path.startswith(RUNNER)}
    runtime = runtime_digest(root)
    stored = StoredStats(root, root, dict(zip(CONTEXT_FIELDS, (SCHEMA, sources, options, runtime))), {})
    modules = {module_name(Path(path)): path for path in paths if Path(path).suffix == ".py"}
    inputs = StatsInputs(root, modules, resources_of([path for path in present if path not in UNKEYED]), {})
    units: list[Unit] = []
    for path in files:
        found = inputs.for_test(path, None)  # pyright: ignore[reportUnknownVariableType, reportUnknownMemberType]
        key = stored.identity(path, found, stored.scope(found), False, inputs.discovery(found))  # pyright: ignore[reportUnknownMemberType]
        units.append(Unit("stats", f"stats-v{digests.SCHEMAS['stats']}-{key}", (path,)))
    return tuple(units)


def mutmut_pin(root: Path) -> str:
    """Return the mutmut version pinned in root's pyproject."""
    pyproject = root / "pyproject.toml"
    match = PIN.search(pyproject.read_text())
    if match is None:
        raise ValueError(f"{pyproject} pins no mutmut version")
    return match[1]


def runner_digest(root: Path) -> str:
    """Digest the mutmut pin with every tracked file of the mutation runner."""
    sources = {path: file_digest(root / path) for path in tracked(root) if path.startswith(RUNNER)}
    return digest([mutmut_pin(root), sources])


def module_digest(source: str) -> str:
    """Digest the module's code outside the functions and methods mutmut trampolines, blind to whitespace and comments."""
    tree = ast.parse(source)
    tree.body = [node for node in tree.body if not isinstance(node, DEFINITIONS)]
    for node in tree.body:
        if isinstance(node, ast.ClassDef):
            node.body = [child for child in node.body if not isinstance(child, DEFINITIONS)]
    return hashlib.sha256(ast.dump(tree).encode()).hexdigest()


def split_members(members: tuple[str, ...], timeout: float) -> list[tuple[str, ...]]:
    """Split a function's mutants, in order, into the fewest near equal parts whose largest prices within timeout at
    MUTANT_SECONDS a mutant, one part when all of them do and one mutant a part when even one prices over it."""
    size = len(members)
    count = next((parts for parts in range(1, size) if -(-size // parts) * MUTANT_SECONDS <= timeout), size)
    return [members[index * size // count : (index + 1) * size // count] for index in range(count)]


def function_units(
    root: Path,
    path: str,
    changed: set[int],
    runner: str,
    covering: Mapping[str, Mapping[str, str]],
    unlisted: Mapping[str, str],
    timeout: float,
) -> list[Unit]:
    """Return the mutant units of each function mutmut trampolines in path whose lines meet changed: one, or the parts
    of split_members keyed by the function key with their own members when its mutants price over timeout. It applies
    no ignore pattern and reads no mutmut config, as the worker's trampolining does."""
    source = (root / path).read_text()
    wrapper, graded, _ = created_mutations(path, source, patterns=())
    starts = wrapper.resolve(WhitespaceInclusivePositionProvider)
    ends = wrapper.resolve(PositionProvider)
    parents = wrapper.resolve(ParentNodeProvider)
    module = module_digest(source)
    units: list[Unit] = []
    for function, mutations in group_by_top_level_node(graded).items():
        if not isinstance(function, cst.FunctionDef) or not changed.intersection(
            range(starts[function].start.line, ends[function].end.line + 1)
        ):
            continue
        owner = parents.get(parents[function])
        class_name = owner.name.value if isinstance(owner, cst.ClassDef) else None
        name = get_mutant_name(Path(path), mangle_function_name(name=function.name.value, class_name=class_name))
        text = wrapper.module.code_for_node(function.with_changes(leading_lines=()))
        key = digest([name, text, module, runner, dict(covering.get(name, {})), dict(unlisted)])
        parts = split_members(tuple(f"{name}__mutmut_{index}" for index in range(1, len(mutations) + 1)), timeout)
        units.extend(
            Unit(
                "mutant",
                f"mutant-v{digests.SCHEMAS['mutant']}-{key if len(parts) == 1 else digest([key, list(part)])}",
                part,
            )
            for part in parts
        )
    return units


def mutant_units(
    root: Path,
    changes: Mapping[str, Iterable[int]],
    covering: Mapping[str, Mapping[str, str]],
    unlisted: Mapping[str, str],
    *,
    timeout: float = math.inf,
) -> tuple[Unit, ...]:
    """Name every mutant of each changed function and key it by the inputs that decide its verdicts.

    changes maps each mutated source file, a Python file present under root, to its changed lines; covering maps a
    qualified mutmut function name to the fingerprints of the tests that cover it; unlisted maps each test module
    under root that holds no manifest test to its hash, together with every file under the measured and test trees
    while such a module exists, and enters every key, since the mutant runner runs it too. timeout is the sandbox
    bound one unit's mutants run under; a function whose mutants price over it gets several units.
    """
    runner = runner_digest(root)
    return tuple(
        unit
        for path in sorted(changes)
        for unit in function_units(root, path, set(changes[path]), runner, covering, unlisted, timeout)
    )


def owner(path: str, unit: str, classes: frozenset[str]) -> str:
    """Return the mutmut name of the top level function or method of path that holds a dependency manifest unit."""
    first, _, rest = unit.partition(".")
    if first in classes:
        return get_mutant_name(Path(path), mangle_function_name(name=rest.partition(".")[0], class_name=first))
    return get_mutant_name(Path(path), mangle_function_name(name=first, class_name=None))


def covering(
    root: Path, manifest: Mapping[str, Any], keys: Mapping[str, str], changes: Iterable[str]
) -> dict[str, dict[str, str]]:
    """Map the mutmut name of each function of the changed files to the fingerprint of every dependency manifest test
    that ran it or a function nested in it."""
    owners: dict[int, str] = {}
    for path in changes:
        tree = ast.parse((root / path).read_text())
        classes = frozenset(node.name for node in tree.body if isinstance(node, ast.ClassDef))
        prefix = f"{path}::"
        for index, name in enumerate(manifest["units"]):
            if name.startswith(prefix):
                owners[index] = owner(path, name.removeprefix(prefix), classes)
    found: dict[str, dict[str, str]] = {}
    for test, record in manifest["tests"].items():
        for index in owners.keys() & set(record["units"]):
            found.setdefault(owners[index], {})[test] = keys[test]
    return found
