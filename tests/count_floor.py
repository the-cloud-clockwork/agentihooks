import argparse
import ast
import hashlib
import importlib.metadata
import json
import os
import re
import subprocess
import sys
import tempfile
from collections.abc import Iterator
from pathlib import Path

import yaml

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
            and (run.get("head_branch") == "dev" or (run.get("head_branch") or "").startswith("gh-readonly-queue/dev/"))
            and run.get("head_sha") == base
            and run.get("repository_id") is not None
            and run.get("repository_id") == run.get("head_repository_id")
        ):
            return artifact["id"]
    return None


def write_record(head: Path, tests: str, out: Path, collected: Path | None = None) -> int:
    identity = collection_identity(head, tests, "")
    if collected is None:
        nodeids = collect_all({"head": head}, tests)["head"]
    else:
        try:
            nodeids = json.loads(collected.read_text())
        except (OSError, ValueError):
            nodeids = None
        if not isinstance(nodeids, list) or not all(isinstance(nodeid, str) for nodeid in nodeids):
            nodeids = None
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


def bound_names(tree: Path) -> tuple[set[tuple[str, str, str]], list[Path]]:
    names, opaque = set(), []
    for path in _sources(tree, set()):
        relative = path.relative_to(tree)
        module_name = _module_name(relative)
        try:
            module = ast.parse(path.read_bytes())
        except (OSError, SyntaxError, ValueError):
            opaque.append(relative)
            continue
        parents = {child: parent for parent in ast.walk(module) for child in ast.iter_child_nodes(parent)}
        for node in ast.walk(module):
            name = None
            if isinstance(node, ast.alias) and node.name == "*":
                opaque.append(relative)
            elif isinstance(node, _DEFINITIONS):
                name = node.name
            elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
                name = node.id
            elif isinstance(node, ast.alias):
                name = node.asname or node.name.split(".")[0]
            elif isinstance(node, (ast.ExceptHandler, ast.MatchAs, ast.MatchStar)) and node.name:
                name = node.name
            elif isinstance(node, ast.MatchMapping) and node.rest:
                name = node.rest
            if name:
                owner = node
                while not isinstance(owner, ast.stmt) and owner in parents:
                    owner = parents[owner]
                names.add((module_name, name, ast.dump(owner)))
    return names, opaque


def _dotted(node: ast.expr) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute) and (prefix := _dotted(node.value)):
        return f"{prefix}.{node.attr}"
    return None


def _imports(
    module: ast.Module | ast.FunctionDef | ast.AsyncFunctionDef,
) -> tuple[dict[str, set[tuple[str, str]]], dict[str, set[str]]]:
    names: dict[str, set[tuple[str, str]]] = {}
    modules: dict[str, set[str]] = {}
    for statement in _body_nodes(module):
        if isinstance(statement, ast.Import):
            for alias in statement.names:
                bound = alias.asname or alias.name.split(".")[0]
                modules.setdefault(bound, set()).add(alias.name if alias.asname else bound)
        elif isinstance(statement, ast.ImportFrom) and statement.module and not statement.level:
            for alias in statement.names:
                bound = alias.asname or alias.name
                names.setdefault(bound, set()).add((statement.module, alias.name))
                modules.setdefault(bound, set()).add(f"{statement.module}.{alias.name}")
    shadowed = _local_bindings(module)
    return (
        {name: subjects for name, subjects in names.items() if name not in shadowed},
        {name: targets for name, targets in modules.items() if name not in shadowed},
    )


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


def _resource_parent(
    node: ast.expr, bindings: dict[str, ast.expr], source: Path, constructors: set[str], seen: frozenset[str]
) -> Path | None:
    if isinstance(node, ast.Attribute) and node.attr == "parent":
        value = _resource_path(node.value, bindings, source, constructors, seen)
        if isinstance(value, Path):
            return value.parent
    if isinstance(node, ast.Subscript) and isinstance(node.value, ast.Attribute) and node.value.attr == "parents":
        value = _resource_path(node.value.value, bindings, source, constructors, seen)
        if isinstance(value, Path) and isinstance(node.slice, ast.Constant):
            index = node.slice.value
            if isinstance(index, int) and 0 <= index < len(value.parents):
                return value.parents[index]
    return None


def _resource_call(
    node: ast.Call, bindings: dict[str, ast.expr], source: Path, constructors: set[str], seen: frozenset[str]
) -> Path | None:
    if node.keywords:
        return None
    if isinstance(node.func, ast.Name) and node.func.id in constructors and len(node.args) == 1:
        value = _resource_path(node.args[0], bindings, source, constructors, seen)
        if isinstance(value, (str, Path)):
            return Path(value)
    if isinstance(node.func, ast.Attribute) and node.func.attr == "resolve" and not node.args:
        value = _resource_path(node.func.value, bindings, source, constructors, seen)
        if isinstance(value, Path):
            return value.resolve()
    return None


def _resource_path(
    node: ast.expr,
    bindings: dict[str, ast.expr],
    source: Path,
    constructors: set[str],
    seen: frozenset[str] = frozenset(),
) -> Path | str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.Name):
        if node.id == "__file__":
            return source
        if node.id in bindings and node.id not in seen:
            return _resource_path(bindings[node.id], bindings, source, constructors, seen | {node.id})
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
        left = _resource_path(node.left, bindings, source, constructors, seen)
        right = _resource_path(node.right, bindings, source, constructors, seen)
        if isinstance(left, Path) and isinstance(right, str):
            return left / right
    if isinstance(node, ast.Call):
        return _resource_call(node, bindings, source, constructors, seen)
    return _resource_parent(node, bindings, source, constructors, seen)


def _fixture_code(path: Path) -> set[str]:
    try:
        fixture = json.loads(path.read_bytes())
    except (OSError, ValueError):
        return set()
    if not isinstance(fixture, dict) or not isinstance(fixture.get("steps"), list):
        return set()
    return {
        step["run"].strip()
        for step in fixture["steps"]
        if isinstance(step, dict) and isinstance(step.get("run"), str) and step["run"].strip()
    }


def _workflow_code(path: Path) -> set[str]:
    try:
        workflow = yaml.safe_load(path.read_bytes())
    except (OSError, ValueError, yaml.YAMLError):
        return set()
    if not isinstance(workflow, dict) or not isinstance(workflow.get("jobs"), dict):
        return set()
    return {
        step["run"].strip()
        for job in workflow["jobs"].values()
        if isinstance(job, dict)
        for step in job.get("steps", [])
        if isinstance(step, dict) and isinstance(step.get("run"), str) and step["run"].strip()
    }


def _executable_code(path: Path) -> set[str]:
    if path.suffix == ".json":
        return _fixture_code(path)
    if path.suffix in (".yml", ".yaml"):
        return _workflow_code(path)
    return set()


def _deleted_fixtures(base: Path, head: Path, referenced: set[tuple[str, str]]) -> set[tuple[str, str]]:
    candidates = {subject for subject in referenced if subject[0] == "@fixture" and not (head / subject[1]).exists()}
    if not candidates:
        return set()
    kept = [
        code
        for path in head.rglob("*")
        if path.suffix in (".json", ".yml", ".yaml") and path.resolve().is_relative_to(head.resolve())
        if (code := _executable_code(path))
    ]
    return {subject for subject in candidates if not any(_executable_code(base / subject[1]) <= code for code in kept)}


def _fixture_subject(
    tree: Path,
    source: Path,
    module: ast.Module,
    call: ast.Call,
    shadowed: set[str],
    function: ast.FunctionDef | ast.AsyncFunctionDef,
) -> set[tuple[str, str]]:
    if not isinstance(call.func, ast.Attribute) or call.func.attr not in {"read_text", "read_bytes"}:
        return set()
    local = _single_bindings(function)
    if any(
        isinstance(node, ast.Name) and node.id in shadowed and node.id not in local
        for node in ast.walk(call.func.value)
    ):
        return set()
    names, _ = _imports(module)
    constructors = {name for name, subjects in names.items() if ("pathlib", "Path") in subjects} - local.keys()
    bindings = {
        statement.targets[0].id: statement.value
        for statement in module.body
        if isinstance(statement, ast.Assign)
        and len(statement.targets) == 1
        and isinstance(statement.targets[0], ast.Name)
    }
    path = _resource_path(call.func.value, {**bindings, **local}, source, constructors)
    if not isinstance(path, Path):
        return set()
    path, root = path.resolve(), tree.resolve()
    if path.is_relative_to(root) and _executable_code(path):
        return {("@fixture", path.relative_to(root).as_posix())}
    return set()


def _single_bindings(function: ast.FunctionDef | ast.AsyncFunctionDef) -> dict[str, ast.expr]:
    """Return each name the function binds exactly once, by a plain assignment, mapped to its assigned value."""
    stores: dict[str, int] = {}
    others = {node.arg for node in ast.walk(function.args) if isinstance(node, ast.arg)}
    for node in _body_nodes(function):
        if isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)):
            stores[node.id] = stores.get(node.id, 0) + 1
        elif isinstance(node, (*_DEFINITIONS, ast.ExceptHandler, ast.MatchAs, ast.MatchStar)) and node.name:
            others.add(node.name)
        elif isinstance(node, ast.MatchMapping) and node.rest:
            others.add(node.rest)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            others.update(alias.asname or alias.name.split(".")[0] for alias in node.names)
    return {
        node.targets[0].id: node.value
        for node in _body_nodes(function)
        if isinstance(node, ast.Assign)
        and len(node.targets) == 1
        and isinstance(node.targets[0], ast.Name)
        and stores[node.targets[0].id] == 1
        and node.targets[0].id not in others
    }


def _entrypoint_subjects(
    tree: Path, call: ast.Call, names: dict[str, set[tuple[str, str]]], modules: dict[str, set[str]]
) -> set[tuple[str, str]]:
    if ("runpy", "run_module") not in _resolve(call.func, names, modules):
        return set()
    target = call.args[0] if len(call.args) == 1 else None
    if not isinstance(target, ast.Constant) or not isinstance(target.value, str):
        return set()
    if not all(part.isidentifier() for part in target.value.split(".")):
        return set()
    if not any(
        keyword.arg == "run_name" and isinstance(keyword.value, ast.Constant) and keyword.value.value == "__main__"
        for keyword in call.keywords
    ):
        return set()
    try:
        module = ast.parse((tree / (target.value.replace(".", "/") + ".py")).read_bytes())
    except (OSError, SyntaxError, ValueError):
        return set()
    return _guard_subjects(module, target.value)


def _guard_subjects(module: ast.Module, module_name: str) -> set[tuple[str, str]]:
    names, modules = _imports(module)
    for node in module.body:
        if isinstance(node, _DEFINITIONS):
            names.setdefault(node.name, set()).add((module_name, node.name))
    guard = ast.dump(ast.parse('__name__ == "__main__"', mode="eval").body)
    return {
        subject
        for node in module.body
        if isinstance(node, ast.If) and ast.dump(node.test) == guard
        for statement in node.body
        for child in ast.walk(statement)
        if isinstance(child, ast.Call)
        for subject in _resolve(child.func, names, modules)
    }


def _body_nodes(function: ast.Module | ast.FunctionDef | ast.AsyncFunctionDef) -> Iterator[ast.AST]:
    pending = list(function.body)
    while pending:
        node = pending.pop()
        yield node
        if not isinstance(node, (*_DEFINITIONS, ast.Lambda)):
            pending.extend(ast.iter_child_nodes(node))


def _local_bindings(
    function: ast.Module | ast.FunctionDef | ast.AsyncFunctionDef, include_definitions: bool = True
) -> set[str]:
    names = (
        {node.arg for node in ast.walk(function.args) if isinstance(node, ast.arg)}
        if isinstance(function, _FUNCTIONS)
        else set()
    )
    for node in _body_nodes(function):
        if isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)):
            names.add(node.id)
        elif isinstance(node, (ast.ExceptHandler, ast.MatchAs, ast.MatchStar)) and node.name:
            names.add(node.name)
        elif isinstance(node, ast.MatchMapping) and node.rest:
            names.add(node.rest)
        elif isinstance(node, _DEFINITIONS) and include_definitions:
            names.add(node.name)
    return names


def referenced_subjects(tree: Path, nodeid: str) -> set[tuple[str, str]]:
    path, *scopes = nodeid.split("[", 1)[0].split("::")
    try:
        module = ast.parse((tree / path).read_bytes())
    except (OSError, SyntaxError, ValueError):
        return set()
    if (node := _test_function(module, scopes)) is None:
        return set()
    names, modules = _imports(module)
    rebound = _local_bindings(module, include_definitions=False) | names.keys() | modules.keys()
    rebound.update(
        alias.asname or alias.name.split(".")[0]
        for statement in _body_nodes(module)
        if isinstance(statement, (ast.Import, ast.ImportFrom))
        for alias in statement.names
    )
    rebound.update(
        child.name for child in _body_nodes(module) if isinstance(child, _DEFINITIONS) and child not in module.body
    )
    helpers = {
        child.name: child for child in module.body if isinstance(child, _FUNCTIONS) and child.name not in rebound
    }
    subjects, visited, pending = set(), set(), [node]
    while pending:
        function = pending.pop()
        if function in visited:
            continue
        visited.add(function)
        shadowed = _local_bindings(function)
        local_names = {name: subjects for name, subjects in names.items() if name not in shadowed}
        local_modules = {name: targets for name, targets in modules.items() if name not in shadowed}
        imported_names, imported_modules = _imports(function)
        shadowed |= imported_names.keys() | imported_modules.keys()
        local_names.update(imported_names)
        local_modules.update(imported_modules)
        for child in _body_nodes(function):
            subjects.update(_resolve(child, local_names, local_modules))
            if isinstance(child, ast.Call):
                subjects.update(_fixture_subject(tree, tree / path, module, child, shadowed, function))
                subjects.update(_entrypoint_subjects(tree, child, local_names, local_modules))
                if (
                    isinstance(child.func, ast.Name)
                    and child.func.id not in shadowed
                    and (helper := helpers.get(child.func.id))
                ):
                    pending.append(helper)
                subjects.update(
                    (callee, f"{function}({keyword.arg})")
                    for callee, function in _resolve(child.func, local_names, local_modules)
                    for keyword in child.keywords
                    if keyword.arg
                )
    return subjects


def _project_module_path(tree: Path, module: str) -> Path | None:
    stem = tree / Path(*module.split("."))
    direct = stem.with_suffix(".py")
    if direct.is_file():
        return direct
    package = stem / "__init__.py"
    return package if package.is_file() else None


def _imported_modules(path: Path) -> set[str]:
    try:
        module = ast.parse(path.read_bytes())
    except (OSError, SyntaxError, ValueError):
        return set()
    modules: set[str] = set()
    for node in ast.walk(module):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
            modules.add(node.module)
            modules.update(f"{node.module}.{alias.name}" for alias in node.names)
    return modules


def _emptied_test_files(base: Path, head: Path, tests: str) -> set[str]:
    emptied = set()
    for path in (base / tests).rglob("*.py"):
        relative = path.relative_to(base)
        if (head / relative).exists():
            continue
        modules = _imported_modules(path)
        if any(_project_module_path(base, module) and not _project_module_path(head, module) for module in modules):
            emptied.add(relative.as_posix())
    return emptied


def _admitted(
    nodeid: str,
    args: argparse.Namespace,
    emptied: set[str],
    deleted: set[tuple[str, str]],
    opaque: list[Path],
    named: dict[str, set[str]],
    retired: dict[str, str],
) -> tuple[list[str], str] | None:
    file = nodeid.split("::", 1)[0]
    if file in emptied:
        return [file], "its test file is deleted together with a module it imported"
    referenced = referenced_subjects(args.base, nodeid)
    removed_fixtures = set() if opaque else _deleted_fixtures(args.base, args.head, referenced)
    if subjects := sorted(".".join(subject) for subject in referenced & (deleted | removed_fixtures)):
        return subjects, f"the head deletes {', '.join(subjects)}, which it referenced"
    if files := _case_files(nodeid, named):
        return files, f"its id names {', '.join(files)}, which the head deletes"
    if reader := retired.get(nodeid):
        return [nodeid], f"the pull request retires it, signed by {reader}"
    return None


_RETIREMENTS = "test-retirements"
_DECIDING = ("APPROVED", "CHANGES_REQUESTED", "DISMISSED")
_SKIPPED = {".git", "__pycache__"}


def _tree_files(tree: Path) -> Iterator[Path]:
    """Yield every regular file a checkout tracks, or of a plain tree outside git metadata and bytecode caches."""
    if (tree / ".git").exists():
        listed = subprocess.run(["git", "ls-files", "-z"], cwd=tree, capture_output=True, check=True).stdout
        paths = (tree / os.fsdecode(name) for name in listed.split(b"\0") if name)
        yield from (path for path in paths if path.is_file() and not path.is_symlink())
        return
    for root, dirs, files in os.walk(tree):
        dirs[:] = [name for name in dirs if name not in _SKIPPED]
        for name in files:
            path = Path(root) / name
            if name not in _SKIPPED and not path.is_symlink():
                yield path


def _deleted_files(base: Path, head: Path) -> dict[str, set[str]]:
    """Map the path, and the file name no head file shares, of each base file the head neither keeps nor renames."""
    head_files = list(_tree_files(head))
    names = {path.name for path in head_files}
    added = [path for path in head_files if not (base / path.relative_to(head)).exists()]
    renamed = {path.suffix for path in added} | {path.stem for path in added}
    candidates = [
        path
        for path in _tree_files(base)
        if not (head / path.relative_to(base)).exists() and not {path.suffix, path.stem} & renamed
    ]
    sizes = {path.stat().st_size for path in candidates}
    kept = {hashlib.sha256(path.read_bytes()).digest() for path in head_files if path.stat().st_size in sizes}
    tokens: dict[str, set[str]] = {}
    for path in candidates:
        if hashlib.sha256(path.read_bytes()).digest() in kept:
            continue
        relative = path.relative_to(base).as_posix()
        for token in {relative} | ({path.name} - names):
            if "/" in token or Path(token).suffix:
                tokens.setdefault(token, set()).add(relative)
    return tokens


def _word_character(character: str) -> bool:
    """Tell whether a character continues a file name or path rather than separating id parts."""
    return bool(character) and (character.isalnum() or character in "_.")


def _bounded(text: str, token: str) -> bool:
    """Tell whether the token occurs in the text with no file name character on either side."""
    start = text.find(token)
    while start >= 0:
        end = start + len(token)
        if not _word_character(text[start - 1 : start]) and not _word_character(text[end : end + 1]):
            return True
        start = text.find(token, start + 1)
    return False


def _case_files(nodeid: str, named: dict[str, set[str]]) -> list[str]:
    """Return the deleted files a lost parametrized case's id names."""
    case = nodeid.partition("[")[2].removesuffix("]")
    return sorted({path for token, paths in named.items() if case and _bounded(case, token) for path in paths})


def retirement_path(root: Path, nodeid: str) -> Path:
    """Return the retirement file of a node id under the tree root."""
    return root / _RETIREMENTS / f"{hashlib.sha256(nodeid.encode('utf-8', 'surrogatepass')).hexdigest()}.json"


def _signed(ruling: object) -> bool:
    """Tell whether a retirement ruling names a Standards reader and gives a reason."""
    return (
        isinstance(ruling, dict)
        and all(isinstance(ruling.get(field), str) and ruling[field].strip() for field in ("reader", "reason"))
        and re.match(r"standards(?![a-z])", ruling["reader"].strip(), re.IGNORECASE) is not None
    )


def retirements(base: Path, head: Path) -> dict[str, str]:
    """Return each node id the head newly retires under a Standards reader's signed ruling, mapped to who signed it.

    While TEST_COUNT_REVIEWED_HEAD names the pull request head, only the retirements a code owner approved are kept.
    """
    retired = {}
    if (head / _RETIREMENTS).is_symlink():
        print(f"{_RETIREMENTS} is a symbolic link, so it admits nothing")
        return retired
    for path in sorted((head / _RETIREMENTS).glob("*.json")):
        try:
            record = None if path.is_symlink() else json.loads(path.read_bytes())
        except (OSError, ValueError):
            record = None
        entry = next(iter(record.items())) if isinstance(record, dict) and len(record) == 1 else None
        if (
            entry is None
            or path.name != retirement_path(head, entry[0]).name
            or not _signed(entry[1])
            or (base / _RETIREMENTS / path.name).exists()
        ):
            print(
                f"{_RETIREMENTS}/{path.name} is not a new retirement signed by a Standards reader, so it admits nothing"
            )
            continue
        retired[entry[0]] = entry[1]["reader"]
    reviewed = os.environ.get("TEST_COUNT_REVIEWED_HEAD")
    reviews = Path(os.environ.get("TEST_COUNT_REVIEWS", ""))
    return _approved(base, retired, reviews, reviewed) if reviewed and retired else retired


def code_owners(root: Path, path: str) -> list[str]:
    """Return the owners named by the last rule of the tree's CODEOWNERS that matches a repository path.

    Rules are read as anchored paths with a star inside one segment and a trailing slash for a folder; one rule
    outside that grammar leaves every path without a known owner.
    """
    rules = root / ".github" / "CODEOWNERS"
    owners: list[str] = []
    for line in rules.read_text().splitlines() if rules.is_file() else []:
        pattern, *named = line.split() or ["#"]
        if pattern.startswith("#"):
            continue
        folder = pattern.endswith("/")
        if (
            not pattern.startswith("/")
            or any(mark in pattern for mark in ("**", "?", "[", "!", "\\"))
            or (not folder and (root / pattern.strip("/")).is_dir())
        ):
            print(f"CODEOWNERS rule {pattern} is outside the grammar the floor matches, so no code owner is known")
            return []
        body = re.escape(pattern.strip("/")).replace(r"\*", "[^/]*") + ("/.*" if folder else "")
        if re.fullmatch(body, path):
            owners = named
    return owners


def approvers(reviews: Path, commit: str) -> set[str]:
    """Return each reviewer, as a lower case @login, whose last deciding review is an approval of the commit.

    Approvals, change requests and dismissals decide; a comment leaves the previous decision standing.
    """
    deciding = {}
    try:
        for line in reviews.read_text().splitlines():
            review = json.loads(line)
            if isinstance(review.get("user"), str) and review.get("state") in _DECIDING:
                deciding[review["user"].lower()] = review
    except (OSError, ValueError, AttributeError):
        print("The pull request reviews could not be read, so no retirement is admitted")
        return set()
    return {
        f"@{user}"
        for user, review in deciding.items()
        if review["state"] == "APPROVED" and review.get("commit_id") == commit
    }


def _approved(base: Path, retired: dict[str, str], reviews: Path, commit: str) -> dict[str, str]:
    """Keep each retirement a code owner of its file approved on the commit, naming that owner beside its reader."""
    approved = approvers(reviews, commit)
    kept = {}
    for nodeid, reader in retired.items():
        name = retirement_path(base, nodeid).relative_to(base).as_posix()
        owners = code_owners(base, name)
        if approving := [owner for owner in owners if owner.lower() in approved]:
            kept[nodeid] = f"{reader} and approved by {approving[0]}"
        else:
            print(
                f"{name} is not approved on the pull request head by a code owner ({', '.join(owners) or 'none'}), "
                "so it admits nothing"
            )
    return kept


def write_retirements(head: Path, nodeids: list[str], reader: str, reason: str) -> int:
    """Write one signed retirement per node id under the head, or refuse a ruling no Standards reader signed."""
    ruling = {"reader": reader, "reason": reason}
    if not _signed(ruling):
        print("::error::A retirement needs a reader naming a Standards reader and a reason.")
        return 1
    targets = [retirement_path(head, nodeid) for nodeid in nodeids]
    if (head / _RETIREMENTS).is_symlink() or any(target.is_symlink() for target in targets):
        print(f"::error::{_RETIREMENTS} holds a symbolic link, so no retirement is written.")
        return 1
    for nodeid in nodeids:
        target = retirement_path(head, nodeid)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps({nodeid: ruling}, indent=2) + "\n")
        print(target.relative_to(head).as_posix())
    return 0


def _admissions(args: argparse.Namespace, lost: list[str]) -> dict[str, list[str]]:
    """Return each lost test an admission rule explains, mapped to the subjects that explain it."""
    surviving, opaque = bound_names(args.head)
    for path in opaque:
        print(
            f"{path} hides its names in the head (unparseable or a star import), "
            "so no lost test is admitted except by a signed retirement"
        )
    definitions = set() if opaque else top_level_definitions(args.base, args.tests)
    previous, _ = bound_names(args.base)
    introduced = {name for _, name, _ in surviving - previous}
    identities = {(module, name) for module, name, _ in surviving}
    deleted = {
        definition for definition in definitions if definition not in identities and definition[1] not in introduced
    }
    deleted |= set() if opaque else deleted_branches(args.base, args.head, args.tests)
    emptied = set() if opaque else _emptied_test_files(args.base, args.head, args.tests)
    named = {} if opaque else _deleted_files(args.base, args.head)
    retired = retirements(args.base, args.head)
    admitted = {}
    for nodeid in lost:
        if admission := _admitted(nodeid, args, emptied, deleted, opaque, named, retired):
            subjects, reason = admission
            admitted[nodeid] = subjects
            print(f"{nodeid} is admitted: {reason}")
    return admitted


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="fail when the head collects fewer tests than the base")
    side = parser.add_mutually_exclusive_group(required=True)
    side.add_argument("--base", type=Path, help="checkout of the base revision")
    side.add_argument("--record", type=Path, help="write the head's collection as a record a later base can reuse")
    side.add_argument("--retire", nargs="+", metavar="NODEID", help="write a signed retirement for each lost test")
    parser.add_argument("--head", type=Path, default=Path.cwd(), help="checkout of the head revision")
    parser.add_argument("--tests", default="tests/")
    parser.add_argument("--collected", type=Path, help="with --record, the head collection the floor already exported")
    parser.add_argument("--reader", default="", help="with --retire, the Standards reader who signs the ruling")
    parser.add_argument("--reason", default="", help="with --retire, why the removed behaviour needs no test")
    args = parser.parse_args(argv)
    if args.retire:
        return write_retirements(args.head, args.retire, args.reader, args.reason)
    if args.record:
        return write_record(args.head, args.tests, args.record, args.collected)
    collected = collect_sides(args.base, args.head, args.tests)
    failed = [side for side, nodeids in collected.items() if not nodeids]
    for side in failed:
        print(f"::error::Collecting the {side} tests failed or found none, so the floor cannot be graded.")
    if failed:
        return 1
    base, head = collected["base"] or [], collected["head"] or []
    if output := os.environ.get("TEST_COUNT_HEAD_COLLECTION"):
        Path(output).write_text(json.dumps(head))
    lost = sorted(set(base) - set(head))
    admitted = _admissions(args, lost) if len(head) < len(base) else {}
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
