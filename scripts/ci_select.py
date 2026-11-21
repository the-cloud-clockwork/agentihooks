import argparse
import ast
import itertools
import json
import math
import os
import re
import subprocess
import tomllib
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, NamedTuple, NotRequired, TypedDict, cast

import yaml

from scripts import locked_tests
from scripts.ci_mutation.scope import select_tests, test_file
from scripts.ci_test_plan import find_record
from scripts.shard_sizing import FIRST_SHARD_FILES, SECONDS_PER_SOURCE_BYTE, assign_work, source_sizes

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
MARKDOWN_GLOB = re.compile(r"glob\(\s*['\"][^'\"]*\*\.md['\"]")
# Changing any of these changes how every test runs, so only the full suite grades them.
HARNESS = frozenset(
    {
        ".github/test-excludes.txt",
        "tests/__init__.py",
        "tests/shards.py",
        "scripts/shard_sizing.py",
        "tests/flake_ledger.py",
        "tests/pass_record.py",
        "tests/dep_inputs.py",
        "tests/browser_coverage.py",
        "tests/node_capture.cjs",
    }
)
CONFTEST = "conftest.py"
PYPROJECT = "pyproject.toml"
# Only the full suite ran these checks, so a new or edited test module could pass its own tier and fail the queue.
SUITE_SPEED = "tests/test_suite_speed.py"
FAKEREDIS = "fakeredis"
# The manifest names its locked tests through a constant, so neither file name selects its agreement check.
LOCKED_CHECK = f"{TESTS}/test_locked_tests.py"
# Only the queue ran these whole tree checks, so a pull request adding, removing or moving a file passed its own tier.
TREE_CHECKS = (f"{TESTS}/test_codeowners.py", f"{TESTS}/test_ci_pull_request_api.py")
TICK_CATALOGUE = f"{TESTS}/test_docs_tick.py"
TICK_STEPS = "scripts/tick/steps/"
WORKFLOW = ".github/workflows/test.yml"
DEFINITIONS = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
FUNCTIONS = (ast.FunctionDef, ast.AsyncFunctionDef)
# Stored test seconds per leg up to the leg limit; a larger selection shares the legs and each runs longer.
LEG_SECONDS = 180.0
UNKNOWN_SECONDS = 10.0


class Record(TypedDict):
    units: list[int]
    files: NotRequired[list[str]]
    outside: NotRequired[list[str]]


class Change(NamedTuple):
    reshaped: bool = False
    pyproject: bool = True
    jobs: set[str] | None = None


def changed_files(root: Path, base: str, head: str) -> list[str]:
    command = ["git", "diff", "--name-only", "--no-renames", "-z", f"{base}...{head}"]
    return [name for name in subprocess.check_output(command, cwd=root).decode().split("\0") if name]


def is_test(path: str) -> bool:
    return path.startswith(f"{TESTS}/") and Path(path).name.startswith("test_") and path.endswith(".py")


def nested_conftest(path: str) -> bool:
    """A conftest below the tests root, whose fixtures reach only the tests beneath its directory."""
    parts = Path(path).parts
    return parts[-1] == CONFTEST and parts[0] == TESTS and len(parts) > 2


def conftest_tests(root: Path, changed: list[str]) -> set[str]:
    """The test files beneath each changed nested conftest."""
    return {
        test.relative_to(root).as_posix()
        for path in changed
        if nested_conftest(path)
        for test in (root / path).parent.rglob("test_*.py")
    }


def harness_tables(root: Path, revision: str) -> tuple[dict[str, object], object, object, object]:
    """The pyproject tables that change how every test runs at a revision, all empty when it has no pyproject."""
    shown = subprocess.run(["git", "show", f"{revision}:{PYPROJECT}"], cwd=root, capture_output=True, text=True)
    data = tomllib.loads(shown.stdout)
    project = data.get("project", {})
    return (
        {name: table for name, table in data.get("tool", {}).items() if name.startswith("pytest")},
        project.get("dependencies"),
        project.get("optional-dependencies"),
        data.get("build-system"),
    )


def pyproject_reaches_tests(root: Path, base: str, head: str) -> bool:
    """Whether the head changes a pyproject table that changes how every test runs, against the merge base."""
    fork = subprocess.check_output(["git", "merge-base", base, head], cwd=root, text=True).strip()
    return harness_tables(root, fork) != harness_tables(root, head)


def changed_jobs(root: Path, base: str, head: str) -> set[str] | None:
    """The Tests workflow jobs whose parsed definition differs between the merge base and the head, None when the
    parsed workflow differs outside its jobs or either side does not parse."""
    fork = subprocess.check_output(["git", "merge-base", base, head], cwd=root, text=True).strip()
    shown = [
        subprocess.run(["git", "show", f"{revision}:{WORKFLOW}"], cwd=root, capture_output=True).stdout
        for revision in (fork, head)
    ]
    try:
        before, after = (cast(dict[Any, Any], yaml.safe_load(text)) for text in shown)
        outside = [{key: value for key, value in side.items() if key != "jobs"} for side in (before, after)]
        if outside[0] != outside[1]:
            return None
        return {
            name for name in {*before["jobs"], *after["jobs"]} if before["jobs"].get(name) != after["jobs"].get(name)
        }
    except (AttributeError, KeyError, TypeError, yaml.YAMLError):
        return None


def needs_full_suite(changed: list[str], pyproject: bool) -> bool:
    return any(
        path in HARNESS
        or (path == PYPROJECT and pyproject)
        or (Path(path).name == CONFTEST and not nested_conftest(path))
        for path in changed
    )


def is_doc(path: str) -> bool:
    return path.startswith(DOCS) or path.endswith(DOC)


def naming_tests(root: Path, needles: set[str]) -> set[str]:
    found: set[str] = set()
    for test in sorted((root / TESTS).rglob("test_*.py")):
        text = test.read_text()
        if any(needle in text for needle in needles):
            found.add(test.relative_to(root).as_posix())
    return found


def markdown_tests(root: Path) -> set[str]:
    """Test files that glob markdown files, which no changed file name reaches."""
    return {
        test.relative_to(root).as_posix()
        for test in sorted((root / TESTS).rglob("test_*.py"))
        if MARKDOWN_GLOB.search(test.read_text())
    }


def constant(node: ast.AST | None) -> str | None:
    return node.value if isinstance(node, ast.Constant) and isinstance(node.value, str) else None


def loads(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "yaml"
        and "load" in node.func.attr
    )


def takes_jobs(node: ast.AST) -> bool:
    if isinstance(node, ast.Subscript):
        return constant(node.slice) == "jobs"
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and constant(next(iter(node.args), None)) == "jobs"
    )


def job_key(node: ast.AST, parents: dict[ast.AST, ast.AST]) -> str | None:
    """The job a use of a jobs mapping names by a constant: an index, the first argument of a method such as get or
    pop, or a membership test; None for any other use."""
    parent = parents.get(node)
    if isinstance(parent, ast.Subscript):
        return constant(parent.slice)
    if isinstance(parent, ast.Attribute) and isinstance(call := parents.get(parent), ast.Call) and call.func is parent:
        return constant(next(iter(call.args), None))
    if (
        isinstance(parent, ast.Compare)
        and parent.comparators == [node]
        and isinstance(parent.ops[0], (ast.In, ast.NotIn))
    ):
        return constant(parent.left)
    return None


def enclosing(node: ast.AST, parents: dict[ast.AST, ast.AST]) -> ast.FunctionDef | ast.AsyncFunctionDef | None:
    chain = [node]
    for item in chain:
        if item in parents:
            chain.append(parents[item])
    return next((item for item in chain if isinstance(item, FUNCTIONS)), None)


def holders(node: ast.AST, parents: dict[ast.AST, ast.AST], nodes: list[ast.AST]) -> list[ast.AST]:
    """The expressions holding the jobs mapping a use hands on: the reads of each name it is assigned to, or the calls
    of the function returning it when that function is only ever called; empty for any other use."""
    parent, scope = parents.get(node), enclosing(node, parents)
    if isinstance(parent, ast.Assign):
        names = {target.id for target in parent.targets if isinstance(target, ast.Name)}
        reads: list[ast.AST] = [
            read for read in (ast.walk(scope) if scope else nodes) if isinstance(read, ast.Name) and read.id in names
        ]
        return reads if len(names) == len(parent.targets) else []
    if isinstance(parent, ast.Return) and scope:
        reads = [read for read in nodes if isinstance(read, ast.Name) and read.id == scope.name]
        calls: list[ast.AST] = [
            call for read in reads if isinstance(call := parents.get(read), ast.Call) and call.func is read
        ]
        return calls if len(calls) == len(reads) else []
    return []


def read_jobs(nodes: list[ast.AST], parents: dict[ast.AST, ast.AST]) -> set[str] | None:
    """The jobs the nodes index by constant name, following every name and function result holding a loaded YAML
    document or a jobs mapping; None when they use a document other than by a constant key, take a jobs mapping any
    other way or index no job, so they read every job."""
    documents = [node for node in nodes if loads(node)]
    for node in itertools.islice(documents, len(nodes)):
        if job_key(node, parents) is None:
            if not (held := holders(node, parents, nodes)):
                return None
            documents.extend(item for item in held if item not in documents)
    pending = [node for node in nodes if takes_jobs(node)]
    jobs: set[str] = set()
    for node in itertools.islice(pending, len(nodes)):
        if (job := job_key(node, parents)) is not None:
            jobs.add(job)
        elif held := holders(node, parents, nodes):
            pending.extend(item for item in held if item not in pending)
        else:
            return None
    return jobs or None


def bindings(tree: ast.Module) -> dict[str, list[ast.stmt]]:
    bound: dict[str, list[ast.stmt]] = {}
    for statement in tree.body:
        if isinstance(statement, DEFINITIONS):
            names = [statement.name]
        else:
            names = [
                node.id
                for node in ast.walk(statement)
                if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store)
            ]
        for name in names:
            bound.setdefault(name, []).append(statement)
    return bound


def reached(definition: ast.stmt, bound: dict[str, list[ast.stmt]], uses: dict[ast.stmt, set[str]]) -> list[ast.stmt]:
    found = [definition]
    for statement in itertools.islice(found, len(uses)):
        found.extend(item for name in uses[statement] for item in bound.get(name, []) if item not in found)
    return found


def definition_jobs(source: str) -> dict[str, set[str] | None]:
    """The Tests workflow jobs each top level test definition reads by constant name, None for one reading every job."""
    tree = ast.parse(source)
    walked: dict[ast.stmt, list[ast.AST]] = {}
    parents: dict[ast.AST, ast.AST] = {}
    for statement in tree.body:
        nodes: list[ast.AST] = [statement]
        walked[statement] = nodes
        for node in nodes:
            children = list(ast.iter_child_nodes(node))
            parents.update(dict.fromkeys(children, node))
            nodes.extend(children)
    uses = {
        statement: {node.id for node in nodes if isinstance(node, ast.Name)}
        | {node.arg for node in nodes if isinstance(node, ast.arg)}
        for statement, nodes in walked.items()
    }
    bound = bindings(tree)
    return {
        statement.name: read_jobs([node for item in reached(statement, bound, uses) for node in walked[item]], parents)
        for statement in tree.body
        if isinstance(statement, DEFINITIONS)
        and statement.name.startswith("Test" if isinstance(statement, ast.ClassDef) else "test")
    }


def workflow_tests(root: Path, jobs: set[str], manifest: dict[str, Any] | None) -> set[str]:
    """The top level test definitions, as file::name, that read a changed Tests workflow job or every job, in each test
    file that names the workflow or that the dev record shows opening it."""
    records = cast(dict[str, Record], manifest["tests"]) if manifest is not None else {}
    opened = {
        test_file(test)
        for test, found in records.items()
        if WORKFLOW in [*found.get("files", []), *found.get("outside", [])]
    }
    tests: set[str] = set()
    for path in sorted(naming_tests(root, {WORKFLOW, Path(WORKFLOW).name}) | opened):
        if (root / path).is_file():
            for name, read in definition_jobs((root / path).read_text()).items():
                if read is None or read & jobs:
                    tests.add(f"{path}::{name}")
    return tests


def reshaped(root: Path, base: str, head: str) -> bool:
    command = ["git", "diff", "--quiet", "--no-renames", "--diff-filter=AD", f"{base}...{head}"]
    result = subprocess.run(command, cwd=root)
    if result.returncode not in (0, 1):
        raise subprocess.CalledProcessError(result.returncode, command)
    return result.returncode == 1


def recorded_tests(manifest: dict[str, Any], changed: list[str]) -> set[str]:
    """Test files whose recorded run executed a changed unit or read a changed file that not every test reads, among
    its recorded files and the files it opened outside its record."""
    names: list[str] = manifest["units"]
    records: dict[str, Record] = manifest["tests"]
    units = {index for index, unit in enumerate(names) if unit.partition("::")[0] in changed}
    lists = {tuple(found.get("files", [])) for found in records.values()}
    read = set(changed) - cast(set[str], cast("Any", set).intersection(*map(set, lists)))
    return {
        test.partition("::")[0]
        for test, found in records.items()
        if units.intersection(found["units"]) or read.intersection([*found.get("files", []), *found.get("outside", [])])
    }


def selected_tests(
    root: Path,
    changed: list[str],
    tree_reshaped: bool,
    manifest: dict[str, Any] | None = None,
    jobs: set[str] | None = None,
) -> set[str]:
    others = [path for path in changed if jobs is None or path != WORKFLOW]
    tests = {path for path in changed if is_test(path)}
    if manifest is not None:
        tests.update(recorded_tests(manifest, others))
    if jobs is not None:
        tests.update(workflow_tests(root, jobs, manifest))
    if tree_reshaped:
        tests.update(TREE_CHECKS)
    tests.update(conftest_tests(root, changed))
    needles: set[str] = set()
    for path in others:
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
    if any(path.endswith(DOC) for path in changed):
        tests |= markdown_tests(root)
    tests = {test for test in tests if SAFE.fullmatch(test_file(test)) and (root / test_file(test)).is_file()}
    if tests and (root / TICK_CATALOGUE).is_file() and any(path.startswith(TICK_STEPS) for path in changed):
        tests.add(TICK_CATALOGUE)
    if (
        tests
        and (root / SUITE_SPEED).is_file()
        and (
            any(is_test(path) for path in changed)
            or any(FAKEREDIS in (root / test_file(test)).read_text() for test in tests)
        )
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
    plain: set[str] = set()
    defined: set[str] = set()
    others: set[str] = set()
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
    stored: set[str] = set()
    for node, duration in durations.items():
        file, _, rest = node.partition("::")
        name = rest.partition("::")[0].partition("[")[0]
        if file == path and name in units:
            units[name] += duration
            stored.add(name)
    if len(units) < 2 or defined - stored - plain or not all(SAFE.fullmatch(name) for name in units):
        return {}
    return {f"{path}::{name}": seconds for name, seconds in units.items()}


def offered_units(root: Path, tests: set[str], durations: dict[str, float]) -> dict[str, float]:
    """The stored seconds of the definitions selected by name, for each file selected only through them that runs its
    definitions apart; such a file that cannot runs whole."""
    units: dict[str, float] = {}
    for path in {test_file(test) for test in tests} - tests - FIRST_SHARD_FILES:
        named = {test for test in tests if test_file(test) == path}
        if named <= (split := file_units(root, path, durations)).keys():
            units.update({unit: split[unit] for unit in named})
    return units


def legs_for(root: Path, tests: set[str], durations: dict[str, float], limit: int) -> list[list[str]]:
    offered = offered_units(root, tests, durations)
    tests = {test_file(test) for test in tests} - {test_file(unit) for unit in offered}
    seconds = file_seconds(durations, tests)
    total = sum(seconds.values()) + sum(offered.values())
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
    work.update(offered)
    sizes.update({unit: (root / test_file(unit)).stat().st_size for unit in offered})
    collection = {unit: sizes[unit] * SECONDS_PER_SOURCE_BYTE for unit in work}
    files_of = {unit: unit.partition("::")[0] for unit in work}
    legs = assign_work(work, work, collection, count, 1, None, files_of)
    legs[0] = sorted(tests & FIRST_SHARD_FILES) + legs[0]
    return [sorted(leg) for leg in legs if leg]


def decide(
    root: Path,
    changed: list[str],
    durations: dict[str, float],
    limit: int,
    change: Change = Change(),
    manifest: dict[str, Any] | None = None,
) -> tuple[str, list[list[str]]]:
    if needs_full_suite(changed, change.pyproject):
        return FULL, []
    tests = selected_tests(root, changed, change.reshaped, manifest, change.jobs)
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
        durations: dict[str, float] = {}
        for path in args.durations or [args.root / DURATIONS]:
            durations.update(json.loads(path.read_text()))
        changed = changed_files(args.root, args.base, args.head)
        pyproject = PYPROJECT in changed and pyproject_reaches_tests(args.root, args.base, args.head)
        manifest = None
        if args.repository and not needs_full_suite(changed, pyproject) and not all(is_doc(path) for path in changed):
            if found := find_record(args.repository, datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")):
                run, manifest = found
                print(f"Recorded coverage from dev run {run['id']} adds runtime dependent tests.")
        jobs = changed_jobs(args.root, args.base, args.head) if WORKFLOW in changed else None
        change = Change(reshaped(args.root, args.base, args.head), pyproject, jobs)
        scope, legs = decide(args.root, changed, durations, args.legs, change, manifest)
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
