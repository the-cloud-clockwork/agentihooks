import argparse
import ast
import hashlib
import importlib.metadata
import json
import os
import subprocess
import sys
import tempfile
from collections.abc import Iterator
from pathlib import Path

_SHARED = {"__init__.py", "conftest.py"}

# Runs inside each tree so the base imports its own sources, never the head's.
_COLLECT = """
import json, os, sys, pytest
from pathlib import Path

class Collected:
    nodeids = []
    root = Path(os.path.abspath(sys.argv[1]))
    group = set(json.loads(sys.argv[3]))

    def pytest_ignore_collect(self, collection_path):
        if collection_path.parent == Collected.root and collection_path.name not in Collected.group:
            return True
        return None

    def pytest_collection_finish(self, session):
        Collected.nodeids = [item.nodeid for item in session.items]

    def pytest_collectreport(self, report: pytest.CollectReport) -> None:
        if report.failed:
            print(report.nodeid, report.longrepr, sep="\\n", file=sys.stderr, flush=True)

code = pytest.main(["--collect-only", "-qq", "--assert=plain", "-p", "no:cacheprovider", sys.argv[1]], plugins=[Collected()])
with open(sys.argv[2], "w") as out:
    ok = code in (pytest.ExitCode.OK, pytest.ExitCode.NO_TESTS_COLLECTED)
    json.dump(Collected.nodeids if ok else None, out)
"""


def collection_identity(root: Path, tests: str, environment: str) -> str | None:
    status = subprocess.run(["git", "status", "--porcelain"], cwd=root, capture_output=True, text=True, check=False)
    if status.returncode or status.stdout:
        return None
    tree = subprocess.check_output(["git", "rev-parse", "HEAD^{tree}"], cwd=root, text=True).strip()
    # The project's own version is derived from its commit; the tree hash already names its code.
    packages = sorted(
        (name, None if name == "agentihooks" else dist.version)
        for dist in importlib.metadata.distributions()
        if (name := dist.metadata["Name"])
    )
    options = {name: value for name, value in os.environ.items() if name.startswith("PYTEST_")}
    identity = json.dumps(
        [tree, tests, environment, sys.version, sys.platform, packages, _COLLECT, options], sort_keys=True
    )
    return hashlib.sha256(identity.encode()).hexdigest()


def collection_cache(root: Path, tests: str, cache: Path | None, environment: str) -> Path | None:
    if cache is None or not environment:
        return None
    key = collection_identity(root, tests, environment)
    return cache / f"{key}.json" if key else None


def recorded_base(record: Path, tree: Path, tests: str) -> list[str] | None:
    try:
        recorded = json.loads(record.read_text())
    except (OSError, ValueError):
        return None
    if not isinstance(recorded, dict) or recorded.get("identity") != collection_identity(tree, tests, ""):
        return None
    nodeids = recorded.get("nodeids")
    return nodeids if isinstance(nodeids, list) and nodeids and all(isinstance(n, str) for n in nodeids) else None


def dev_record(listing: dict, base: str) -> int | None:
    for artifact in listing.get("artifacts", []):
        run = artifact.get("workflow_run") or {}
        if (
            not artifact.get("expired")
            and run.get("head_branch") == "dev"
            and run.get("head_sha") == base
            and run.get("repository_id") is not None
            and run.get("repository_id") == run.get("head_repository_id")
        ):
            return artifact["id"]
    return None


def write_record(head: Path, tests: str, out: Path) -> int:
    identity = collection_identity(head, tests, "")
    nodeids = collect_all({"head": head}, tests)["head"]
    if not nodeids or identity is None or identity != collection_identity(head, tests, ""):
        print("::error::The tree did not collect cleanly, so no record is written.")
        return 1
    out.write_text(json.dumps({"identity": identity, "nodeids": nodeids}))
    print(f"recorded {len(nodeids)} tests")
    return 0


def base_from_record(base: Path, tests: str) -> list[str] | None:
    record = os.environ.get("TEST_COUNT_BASE_RECORD")
    if not record:
        return None
    recorded = recorded_base(Path(record), base, tests)
    print("base count read from the dev record" if recorded else "no dev record matches the base, so it is collected")
    return recorded


def collect_sides(base: Path, head: Path, tests: str) -> dict[str, list[str] | None]:
    recorded = base_from_record(base, tests)
    cache = os.environ.get("TEST_COUNT_COLLECTION_CACHE")
    collected = collect_all(
        {"head": head} if recorded else {"base": base, "head": head},
        tests,
        cache=Path(cache) if cache else None,
        environment=os.environ.get("TEST_COUNT_ENVIRONMENT", ""),
    )
    return {"base": recorded, **collected} if recorded else collected


def collect_all(
    trees: dict[str, Path], tests: str, cache: Path | None = None, environment: str = ""
) -> dict[str, list[str] | None]:
    cached = {side: collection_cache(tree, tests, cache, environment) for side, tree in trees.items()}
    collected = {}
    for side in trees:
        if cached[side] and cached[side].is_file():
            nodeids = json.loads(cached[side].read_text())
            collected[side] = (
                nodeids if isinstance(nodeids, list) and all(isinstance(nodeid, str) for nodeid in nodeids) else None
            )
    pending = [side for side in trees if side not in collected]
    processes = collection_processes() // max(1, len(pending))
    with tempfile.TemporaryDirectory() as scratch:
        runs = {}
        for side in pending:
            runs[side] = []
            for index, group in enumerate(collection_groups(trees[side] / tests, processes)):
                output = Path(scratch) / f"{side}-{index}.json"
                command = [sys.executable, "-c", _COLLECT, tests, str(output), json.dumps(group)]
                runs[side].append((subprocess.Popen(command, cwd=trees[side], stdout=subprocess.DEVNULL), output))
        for side, parts in runs.items():
            results = []
            for run, output in parts:
                run.wait()
                results.append(json.loads(output.read_text()) if output.is_file() and not run.returncode else None)
            nodeids = None if any(result is None for result in results) else sum(results, [])
            # Groups are disjoint, so a node id from two groups means the split leaked and would inflate the count.
            if nodeids and len(set(nodeids)) != sum(len(set(result)) for result in results):
                nodeids = None
            collected[side] = nodeids
            if collected[side] and cached[side]:
                cached[side].parent.mkdir(parents=True, exist_ok=True)
                cached[side].write_text(json.dumps(collected[side]))
    return collected


def collection_processes() -> int:
    # Imported here: the dev record fetch imports this module from a base tree that may hold only the tests.
    from scripts.ci_cpu_quota import quota_cpus, worker_count

    return worker_count(len(os.sched_getaffinity(0)), quota_cpus())


def collection_groups(root: Path, count: int) -> list[list[str]]:
    entries = sorted(root.iterdir(), key=lambda path: path.name) if root.is_dir() else []
    entries = [path for path in entries if path.name not in _SHARED]
    weights = [
        sum(1 for _ in path.rglob("test_*.py"))
        if path.is_dir()
        else int(path.name.startswith("test_") and path.suffix == ".py")
        for path in entries
    ]
    total, done, groups = sum(weights), 0, [[]]
    for path, weight in zip(entries, weights):
        if weight and done * count >= total * len(groups):
            groups.append([])
        groups[-1].append(path.name)
        done += weight
    return [sorted(_SHARED) + group for group in groups]


_DEFINITIONS = (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)


def _definitions(body: list[ast.stmt]) -> Iterator[str]:
    for node in body:
        if isinstance(node, _DEFINITIONS):
            yield node.name
        elif isinstance(node, ast.Assign):
            yield from (target.id for target in node.targets if isinstance(target, ast.Name))
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            yield node.target.id
        elif isinstance(node, (ast.If, ast.Try)):
            yield from _definitions(node.body + node.orelse + getattr(node, "finalbody", []))
            for handler in getattr(node, "handlers", []):
                yield from _definitions(handler.body)


def _module_name(relative: Path) -> str:
    parts = relative.with_suffix("").parts
    return ".".join(parts[:-1] if parts[-1] == "__init__" else parts)


def _sources(tree: Path, skipped: set[Path]) -> Iterator[Path]:
    for root, dirs, files in os.walk(tree):
        dirs[:] = [name for name in dirs if name != ".git" and (Path(root) / name).resolve() not in skipped]
        yield from (Path(root) / name for name in files if name.endswith(".py"))


def _top_level(tree: Path, tests: str) -> Iterator[tuple[str, list[ast.stmt]]]:
    for path in _sources(tree, {(tree / tests).resolve()}):
        try:
            body = ast.parse(path.read_bytes()).body
        except (OSError, SyntaxError, ValueError):
            continue
        yield _module_name(path.relative_to(tree)), body


def top_level_definitions(tree: Path, tests: str) -> set[tuple[str, str]]:
    return {(module, definition) for module, body in _top_level(tree, tests) for definition in _definitions(body)}


_FUNCTIONS = (ast.FunctionDef, ast.AsyncFunctionDef)


def _parameters(function: ast.FunctionDef | ast.AsyncFunctionDef) -> set[str]:
    arguments = function.args
    variadic = [arguments.vararg] if arguments.vararg else []
    return {argument.arg for argument in arguments.posonlyargs + arguments.args + arguments.kwonlyargs + variadic}


def _branch_parameters(function: ast.FunctionDef | ast.AsyncFunctionDef) -> set[str]:
    conditions = (node.test for node in ast.walk(function) if isinstance(node, (ast.If, ast.IfExp, ast.While)))
    used = {name.id for condition in conditions for name in ast.walk(condition) if isinstance(name, ast.Name)}
    rebound = {name.id for name in ast.walk(function) if isinstance(name, ast.Name) and isinstance(name.ctx, ast.Store)}
    return _parameters(function) & (used - rebound)


def deleted_branches(base: Path, head: Path, tests: str) -> set[tuple[str, str]]:
    kept = {
        (module, node.name): node
        for module, body in _top_level(head, tests)
        for node in body
        if isinstance(node, _FUNCTIONS)
    }
    deleted = set()
    for module, body in _top_level(base, tests):
        for node in body:
            if not isinstance(node, _FUNCTIONS) or (survivor := kept.get((module, node.name))) is None:
                continue
            if survivor.args.kwarg or _parameters(survivor) - _parameters(node):
                continue
            deleted.update(
                (module, f"{node.name}({name})") for name in _branch_parameters(node) - _parameters(survivor)
            )
    return deleted


def bound_names(tree: Path) -> tuple[set[str], list[Path]]:
    names, opaque = set(), []
    for path in _sources(tree, set()):
        try:
            module = ast.parse(path.read_bytes())
        except (OSError, SyntaxError, ValueError):
            opaque.append(path.relative_to(tree))
            continue
        for node in ast.walk(module):
            if isinstance(node, ast.alias) and node.name == "*":
                opaque.append(path.relative_to(tree))
            elif isinstance(node, _DEFINITIONS):
                names.add(node.name)
            elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
                names.add(node.id)
            elif isinstance(node, ast.alias):
                names.add(node.asname or node.name.split(".")[0])
            elif isinstance(node, (ast.ExceptHandler, ast.MatchAs, ast.MatchStar)) and node.name:
                names.add(node.name)
            elif isinstance(node, ast.MatchMapping) and node.rest:
                names.add(node.rest)
    return names, opaque


def _dotted(node: ast.expr) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute) and (prefix := _dotted(node.value)):
        return f"{prefix}.{node.attr}"
    return None


def _imports(module: ast.Module) -> tuple[dict[str, set[tuple[str, str]]], dict[str, set[str]]]:
    names: dict[str, set[tuple[str, str]]] = {}
    modules: dict[str, set[str]] = {}
    for statement in ast.walk(module):
        if isinstance(statement, ast.Import):
            for alias in statement.names:
                bound = alias.asname or alias.name.split(".")[0]
                modules.setdefault(bound, set()).add(alias.name if alias.asname else bound)
        elif isinstance(statement, ast.ImportFrom) and statement.module and not statement.level:
            for alias in statement.names:
                bound = alias.asname or alias.name
                names.setdefault(bound, set()).add((statement.module, alias.name))
                modules.setdefault(bound, set()).add(f"{statement.module}.{alias.name}")
    return names, modules


def _test_function(module: ast.Module, scopes: list[str]) -> ast.FunctionDef | ast.AsyncFunctionDef | None:
    body, node = module.body, None
    for scope in scopes:
        node = next((child for child in body if isinstance(child, _DEFINITIONS) and child.name == scope), None)
        if node is None:
            return None
        body = node.body
    return None if isinstance(node, ast.ClassDef) else node


def _resolve(
    node: ast.expr, names: dict[str, set[tuple[str, str]]], modules: dict[str, set[str]]
) -> set[tuple[str, str]]:
    if isinstance(node, ast.Name):
        return names.get(node.id, set())
    if isinstance(node, ast.Attribute) and (prefix := _dotted(node.value)):
        root, _, rest = prefix.partition(".")
        return {(f"{bound}.{rest}" if rest else bound, node.attr) for bound in modules.get(root, ())}
    return set()


def referenced_subjects(tree: Path, nodeid: str) -> set[tuple[str, str]]:
    path, *scopes = nodeid.split("[", 1)[0].split("::")
    try:
        module = ast.parse((tree / path).read_bytes())
    except (OSError, SyntaxError, ValueError):
        return set()
    if (node := _test_function(module, scopes)) is None:
        return set()
    names, modules = _imports(module)
    subjects = set()
    for child in (descendant for statement in node.body for descendant in ast.walk(statement)):
        subjects.update(_resolve(child, names, modules))
        if isinstance(child, ast.Call):
            subjects.update(
                (callee, f"{function}({keyword.arg})")
                for callee, function in _resolve(child.func, names, modules)
                for keyword in child.keywords
                if keyword.arg
            )
    return subjects


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="fail when the head collects fewer tests than the base")
    side = parser.add_mutually_exclusive_group(required=True)
    side.add_argument("--base", type=Path, help="checkout of the base revision")
    side.add_argument("--record", type=Path, help="write the head's collection as a record a later base can reuse")
    parser.add_argument("--head", type=Path, default=Path.cwd(), help="checkout of the head revision")
    parser.add_argument("--tests", default="tests/")
    args = parser.parse_args(argv)
    if args.record:
        return write_record(args.head, args.tests, args.record)
    collected = collect_sides(args.base, args.head, args.tests)
    failed = [side for side, nodeids in collected.items() if not nodeids]
    for side in failed:
        print(f"::error::Collecting the {side} tests failed or found none, so the floor cannot be graded.")
    if failed:
        return 1
    base, head = collected["base"], collected["head"]
    if output := os.environ.get("TEST_COUNT_HEAD_COLLECTION"):
        Path(output).write_text(json.dumps(head))
    lost = sorted(set(base) - set(head))
    admitted = {}
    if len(head) < len(base):
        surviving, opaque = bound_names(args.head)
        for path in opaque:
            print(f"{path} hides its names in the head (unparseable or a star import), so no lost test is admitted")
        definitions = set() if opaque else top_level_definitions(args.base, args.tests)
        deleted = {definition for definition in definitions if definition[1] not in surviving}
        deleted |= set() if opaque else deleted_branches(args.base, args.head, args.tests)
        for nodeid in lost:
            if subjects := sorted(".".join(subject) for subject in referenced_subjects(args.base, nodeid) & deleted):
                admitted[nodeid] = subjects
                print(f"{nodeid} is admitted: the head deletes {', '.join(subjects)}, which it referenced")
    floor = len(base) - len(admitted)
    print(f"head collects {len(head)} tests, base floor {floor}")
    if len(head) < floor:
        for nodeid in lost:
            if nodeid not in admitted:
                print(f"{nodeid} is in the base and not in the head")
        print(f"::error::The head collects {floor - len(head)} fewer tests than the base floor of {floor}.")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
