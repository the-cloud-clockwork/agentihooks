import ast
import functools
import hashlib
import json
import subprocess
import sys
from collections.abc import Callable
from importlib import metadata
from pathlib import Path

from scripts.ci_mutation.scope import imported_names, module_name, selects, test_file

SCHEMA = 3
CONTEXT_FIELDS = ("schema", "sources", "options", "runtime")
KEY_FIELDS = ("path", "tests")
DYNAMIC_IMPORTS = ("import_module", "__import__", "spec_from_file_location", "module_from_spec")
CONFIGURATION = {"pyproject.toml", "pytest.ini", "setup.cfg", "tox.ini", "uv.lock", ".coveragerc"}
TRACKED = ("git", "ls-files", "-z")
report = functools.partial(print, flush=True)
encode = functools.partial(json.dumps, sort_keys=True)


def loader_name(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def test_inputs(
    root: Path,
    path: str,
    modules: dict[str, str],
    records: dict[str, tuple[str, set[str]]],
    resources: dict[str, set[str]],
) -> dict[str, str]:
    pending = {path} | {
        str(parent / "conftest.py") for parent in Path(path).parents if str(parent / "conftest.py") in modules.values()
    }
    inputs = {}
    while pending:
        current = pending.pop()
        if current not in records:
            content = (root / current).read_bytes()
            dependencies = set()
            if Path(current).suffix == ".py":
                text = content.decode()
                nodes = list(ast.walk(ast.parse(text)))
                names = set(imported_names(text, Path(current).parent.parts))
                loaders = {name: name for name in DYNAMIC_IMPORTS} | {
                    alias.asname or alias.name: alias.name
                    for node in nodes
                    if isinstance(node, ast.ImportFrom) and node.module in ("importlib", "importlib.util", "builtins")
                    for alias in node.names
                    if alias.name in DYNAMIC_IMPORTS
                }
                dynamic = [node for node in nodes if isinstance(node, ast.Call) and loader_name(node.func) in loaders]
                called = {id(node.func) for node in dynamic}
                if any(id(node) not in called and loader_name(node) in loaders for node in nodes):
                    dependencies.update(modules.values())
                for node in dynamic:
                    if (
                        node.args
                        and isinstance(node.args[0], ast.Constant)
                        and isinstance(node.args[0].value, str)
                        and not node.args[0].value.startswith(".")
                        and loaders[loader_name(node.func)] != "spec_from_file_location"
                    ):
                        names.add(node.args[0].value)
                    else:
                        dependencies.update(modules.values())
                for name in names:
                    parts = []
                    for part in name.split("."):
                        parts.append(part)
                        prefix = ".".join(parts)
                        if prefix in modules:
                            dependencies.add(modules[prefix])
                literals = {
                    node.value
                    for node in nodes
                    if isinstance(node, ast.Constant) and isinstance(node.value, str) and node.value
                }
                dependencies.update(resource for value in literals for resource in resources.get(value, ()))
            records[current] = (hashlib.sha256(content).hexdigest(), dependencies)
        digest, dependencies = records[current]
        inputs[current] = digest
        pending.update(dependencies - inputs.keys())
    return inputs


def collect_cached_stats(
    root: Path,
    folder: Path,
    changes: dict,
    files: list[str],
    options: list[str],
    collect: Callable[[list[str]], list[dict]],
) -> list[dict]:
    tracked = subprocess.check_output(TRACKED, cwd=root, text=True).split("\0")
    sources = {
        path: hashlib.sha256((root / path).read_bytes()).hexdigest()
        for path in tracked
        if path and (path in CONFIGURATION or path.startswith("scripts/ci_mutation/")) and (root / path).is_file()
    }
    versions = {distribution.metadata["Name"]: distribution.version for distribution in metadata.distributions()}
    context = dict(zip(CONTEXT_FIELDS, (SCHEMA, sources, options, [sys.version, sys.platform, versions])))
    modules = {module_name(Path(path)): path for path in tracked if Path(path).suffix == ".py"}
    records = {}
    folder.mkdir(parents=True, exist_ok=True)
    resources = {}
    for path in tracked:
        if path and (root / path).is_file():
            parts = Path(path).parts
            for index in range(len(parts)):
                resources.setdefault("/".join(parts[index:]), set()).add(path)
    traced = {}
    for name in dict.fromkeys(map(test_file, files)):
        traced[name] = test_inputs(root, name, modules, records, resources)
    inputs = {path: dict(traced[test_file(path)]) for path in files}
    for dependencies in inputs.values():
        if not any(not name.startswith("tests/") for name in dependencies):
            for source in changes.keys() & resources:
                dependencies.update(test_inputs(root, source, modules, records, resources))
    keys = {
        path: hashlib.sha256(
            encode(
                {
                    **context,
                    **dict(zip(KEY_FIELDS, (path, dependencies))),
                    **{source: lines for source, lines in changes.items() if source in dependencies},
                }
            ).encode()
        ).hexdigest()
        for path, dependencies in inputs.items()
    }
    stored = {path: folder / f"{key}.json" for path, key in keys.items()}
    reused = [json.loads(file.read_text()) for file in stored.values() if file.is_file()]
    missing = [path for path, file in stored.items() if not file.is_file()]
    report(f"Mutation stats cache: reused {len(reused)} test files; collect {len(missing)}")
    fresh = collect(missing) if missing else []
    if any(result["status"] != 0 for result in fresh):
        raise SystemExit(1)
    for path in missing:
        durations = {
            test: seconds for result in fresh for test, seconds in result["durations"].items() if selects(path, test)
        }
        tests = {
            function: sorted(
                {test for result in fresh for test in result["tests"].get(function, []) if test in durations}
            )
            for function in {function for result in fresh for function in result["tests"]}
        }
        (folder / f"{keys[path]}.json").write_text(
            json.dumps(
                {
                    "status": 0,
                    "tests": tests,
                    "durations": durations,
                    "cpu": sum(result["cpu"] for result in fresh) / len(missing),
                }
            )
        )
    return reused + fresh
