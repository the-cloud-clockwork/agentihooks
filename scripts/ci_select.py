import argparse
import ast
import json
import math
import os
import re
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import NotRequired, TypedDict, cast

from scripts import locked_tests
from scripts.ci_mutation.scope import select_tests
from scripts.ci_test_plan import find_record
from tests.shards import FIRST_SHARD_FILES, SECONDS_PER_SOURCE_BYTE, _assign_work, source_sizes

PULL_REQUEST = "pull_request"
FULL = "full"
SELECTED = "selected"
NONE = "none"
DURATIONS = ".test_durations"
TESTS = "tests"
SOURCES = ("hooks", "scripts")
DOCS = "docs/"
DOC = ".md"
# A name the workflow passes to pytest through one space separated output line.
SAFE = re.compile(r"[A-Za-z0-9_./-]+")
# Changing any of these changes how every test runs, so only the full suite grades them.
HARNESS = frozenset(
    {
        "pyproject.toml",
        ".github/test-excludes.txt",
        "tests/__init__.py",
        "tests/shards.py",
        "tests/flake_ledger.py",
        "tests/pass_record.py",
        "tests/dep_inputs.py",
        "tests/browser_coverage.py",
        "tests/node_capture.cjs",
    }
)
CONFTEST = "conftest.py"
# Only the full suite ran these checks, so a new or edited test module could pass its own tier and fail the queue.
SUITE_SPEED = "tests/test_suite_speed.py"
FAKEREDIS = "fakeredis"
# The manifest names its locked tests through a constant, so neither file name selects its agreement check.
LOCKED_CHECK = f"{TESTS}/test_locked_tests.py"
# Only the queue ran these whole tree checks, so a pull request adding, removing or moving a file passed its own tier.
TREE_CHECKS = (f"{TESTS}/test_codeowners.py", f"{TESTS}/test_ci_pull_request_api.py")
# Stored test seconds per leg up to the leg limit; a larger selection shares the legs and each runs longer.
LEG_SECONDS = 180.0
UNKNOWN_SECONDS = 10.0


class Record(TypedDict):
    units: list[int]
    files: NotRequired[list[str]]


def changed_files(root: Path, base: str, head: str) -> list[str]:
    command = ["git", "diff", "--name-only", "--no-renames", "-z", f"{base}...{head}"]
    return [name for name in subprocess.check_output(command, cwd=root).decode().split("\0") if name]


def is_test(path: str) -> bool:
    return path.startswith(f"{TESTS}/") and Path(path).name.startswith("test_") and path.endswith(".py")


def needs_full_suite(changed: list[str]) -> bool:
    return any(path in HARNESS or Path(path).name == CONFTEST for path in changed)


def is_doc(path: str) -> bool:
    return path.startswith(DOCS) or path.endswith(DOC)


def naming_tests(root: Path, needles: set[str]) -> set[str]:
    found = set()
    for test in sorted((root / TESTS).rglob("test_*.py")):
        text = test.read_text()
        if any(needle in text for needle in needles):
            found.add(test.relative_to(root).as_posix())
    return found


def reshaped(root: Path, base: str, head: str) -> bool:
    command = ["git", "diff", "--quiet", "--no-renames", "--diff-filter=AD", f"{base}...{head}"]
    result = subprocess.run(command, cwd=root)
    if result.returncode not in (0, 1):
        raise subprocess.CalledProcessError(result.returncode, command)
    return result.returncode == 1


def recorded_tests(manifest: dict[str, object], changed: list[str]) -> set[str]:
    """Test files whose recorded run executed a changed unit or read a changed file that not every test reads."""
    names = cast("list[str]", manifest["units"])
    records = cast("dict[str, Record]", manifest["tests"])
    units = {index for index, unit in enumerate(names) if unit.partition("::")[0] in changed}
    lists = {tuple(found.get("files", [])) for found in records.values()}
    read = set(changed) - set.intersection(*map(set, lists))
    return {
        test.partition("::")[0]
        for test, found in records.items()
        if units.intersection(found["units"]) or read.intersection(found.get("files", []))
    }


def selected_tests(root: Path, changed: list[str], tree_reshaped: bool, manifest: dict | None = None) -> set[str]:
    tests = {path for path in changed if is_test(path)}
    if manifest is not None:
        tests.update(recorded_tests(manifest, changed))
    if tree_reshaped:
        tests.update(TREE_CHECKS)
    needles = set()
    for path in changed:
        needles.add(path)
        if Path(path).suffix == ".js" or Path(path).suffix == ".py" and Path(path).parts[0] in (*SOURCES, TESTS):
            tests.update(select_tests(root, Path(path)))
        else:
            needles.add(Path(path).name)
    if any(
        path.startswith(".github/workflows/")
        or path in {".github/runner-classes.json", "scripts/ci_runner_classes.py", "scripts/ci_runner_trust.py"}
        for path in changed
    ):
        tests.update(
            {
                "tests/test_ci_anton_runners.py",
                "tests/test_ci_runner_classes.py",
                "tests/test_ci_runner_trust.py",
            }
        )
    if any(path in locked_tests.LOCKED or path == locked_tests.MANIFEST.as_posix() for path in changed):
        tests.add(LOCKED_CHECK)
    tests |= naming_tests(root, needles)
    tests = {path for path in tests if SAFE.fullmatch(path) and (root / path).is_file()}
    if (
        tests
        and (root / SUITE_SPEED).is_file()
        and (any(is_test(path) for path in changed) or any(FAKEREDIS in (root / path).read_text() for path in tests))
    ):
        tests.add(SUITE_SPEED)
    return tests


def file_seconds(durations: dict[str, float], files: set[str]) -> dict[str, float]:
    seconds = dict.fromkeys(files, 0.0)
    for node, duration in durations.items():
        path = node.partition("::")[0]
        if path in seconds:
            seconds[path] += duration
    return {path: total if total else UNKNOWN_SECONDS for path, total in seconds.items()}


def defined_tests(source: str) -> tuple[set[str], set[str], set[str]] | None:
    """Top level test definitions, those with only mark decorators, and every other name the module binds; None when
    pytest might collect a test those names miss or refuse a class node, or the file shares xdist group state."""
    if "xdist_group" in source:
        return None
    plain = set()
    defined = set()
    others = set()
    for statement in ast.parse(source).body:
        if isinstance(statement, ast.ClassDef) and (
            statement.bases
            or statement.keywords
            or any(isinstance(node, ast.FunctionDef) and node.name == "__init__" for node in statement.body)
        ):
            return None
        if isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            others.add(statement.name)
            if statement.name.startswith("Test" if isinstance(statement, ast.ClassDef) else "test"):
                defined.add(statement.name)
            if all(ast.unparse(decorator).startswith("pytest.mark.") for decorator in statement.decorator_list):
                plain.add(statement.name)
            continue
        for node in ast.walk(statement):
            if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
                others.add(node.id)
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                others.add(node.name)
            elif isinstance(node, (ast.Import, ast.ImportFrom)):
                others.update(alias.asname or alias.name for alias in node.names)
    return defined, plain - {name for name in defined if name.startswith("Test")}, others - defined


def file_units(root: Path, path: str, durations: dict[str, float]) -> dict[str, float]:
    """Seconds per top level test definition of a file, empty unless those definitions are every test name the module
    binds and each unstored one is a plain function pytest surely collects."""
    names = defined_tests((root / path).read_text())
    if names is None:
        return {}
    defined, plain, others = names
    if "*" in others or any(name.startswith(("test", "Test")) for name in others):
        return {}
    units = dict.fromkeys(defined, 0.0)
    stored = set()
    for node, duration in durations.items():
        file, _, rest = node.partition("::")
        name = rest.partition("::")[0].partition("[")[0]
        if file == path and name in units:
            units[name] += duration
            stored.add(name)
    if len(units) < 2 or defined - stored - plain or not all(SAFE.fullmatch(name) for name in units):
        return {}
    return {f"{path}::{name}": seconds for name, seconds in units.items()}


def legs_for(root: Path, tests: set[str], durations: dict[str, float], limit: int) -> list[list[str]]:
    seconds = file_seconds(durations, tests)
    total = sum(seconds.values())
    count = min(limit, max(1, math.ceil(total / LEG_SECONDS)))
    files = sorted(tests - FIRST_SHARD_FILES)
    sizes = source_sizes(root, files)
    work = dict.fromkeys(files, 0.0)
    for node, duration in durations.items():
        if (path := node.partition("::")[0]) in work:
            work[path] += duration
    for path in files:
        if seconds[path] > total / count and (units := file_units(root, path, durations)):
            del work[path]
            work.update(units)
            sizes.update(dict.fromkeys(units, sizes.pop(path)))
    collection = {unit: sizes[unit] * SECONDS_PER_SOURCE_BYTE for unit in work}
    files_of = {unit: unit.partition("::")[0] for unit in work}
    legs = _assign_work(work, work, collection, count, 1, None, files_of)
    legs[0] = sorted(tests & FIRST_SHARD_FILES) + legs[0]
    return [sorted(leg) for leg in legs if leg]


def decide(
    root: Path,
    changed: list[str],
    durations: dict[str, float],
    limit: int,
    tree_reshaped: bool = False,
    manifest: dict | None = None,
) -> tuple[str, list[list[str]]]:
    if needs_full_suite(changed):
        return FULL, []
    tests = selected_tests(root, changed, tree_reshaped, manifest)
    if not tests:
        return (NONE if all(is_doc(path) for path in changed) else FULL), []
    return SELECTED, legs_for(root, tests, durations, limit)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", required=True)
    parser.add_argument("--head", required=True)
    parser.add_argument("--event", required=True)
    parser.add_argument("--legs", type=int, required=True)
    parser.add_argument("--root", type=Path, default=Path("."))
    parser.add_argument("--durations", type=Path, nargs="*")
    parser.add_argument("--repository", default=os.environ.get("CI_SELECT_REPOSITORY"))
    args = parser.parse_args(argv)
    scope, legs = FULL, []
    if args.event == PULL_REQUEST:
        durations = {}
        for path in args.durations or [args.root / DURATIONS]:
            durations.update(json.loads(path.read_text()))
        changed = changed_files(args.root, args.base, args.head)
        manifest = None
        if args.repository and not needs_full_suite(changed) and not all(is_doc(path) for path in changed):
            if found := find_record(args.repository, datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")):
                run, manifest = found
                print(f"Recorded coverage from dev run {run['id']} adds runtime dependent tests.")
        scope, legs = decide(
            args.root, changed, durations, args.legs, reshaped(args.root, args.base, args.head), manifest
        )
    outputs = {"scope": scope, "legs": json.dumps(list(range(1, len(legs) + 1))), "count": str(len(legs))}
    outputs.update({f"files_{index}": " ".join(leg) for index, leg in enumerate(legs, 1)})
    for key, value in outputs.items():
        print(f"{key}={value}")
    if destination := os.environ.get("GITHUB_OUTPUT"):
        with Path(destination).open("a") as output:
            output.write("".join(f"{key}={value}\n" for key, value in outputs.items()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
