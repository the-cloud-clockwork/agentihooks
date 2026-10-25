import ast
import functools
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tomllib
from collections.abc import Callable
from dataclasses import dataclass, field
from importlib import metadata
from pathlib import Path

from scripts.ci_mutation.scope import imported_names, module_name, selects, test_file

SCHEMA = 4
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
    records: dict[str, tuple[str, set[str], list[ast.AST]]],
    resources: dict[str, set[str]],
    dynamic_imports: bool = True,
) -> dict[str, str]:
    dynamic_modules = modules.values() if dynamic_imports else ()
    pending = {path} | {
        str(parent / "conftest.py") for parent in Path(path).parents if str(parent / "conftest.py") in modules.values()
    }
    inputs = {}
    while pending:
        current = pending.pop()
        if current not in records:
            content = (root / current).read_bytes()
            dependencies = set()
            unresolved = []
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
                unresolved = [node for node in nodes if id(node) not in called and loader_name(node) in loaders]
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
                        unresolved.append(node)
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
            if unresolved:
                dependencies.update(dynamic_modules)
            records[current] = (hashlib.sha256(content).hexdigest(), dependencies, unresolved)
        digest, dependencies, _ = records[current]
        inputs[current] = digest
        pending.update(dependencies - inputs.keys())
    return inputs


def current_input(root: Path, changes: dict, digests: dict[str, str | None], name: str) -> list:
    if name not in digests:
        file = root / name
        digests[name] = hashlib.sha256(file.read_bytes()).hexdigest() if file.is_file() else None
    return [digests[name], changes.get(name)]


def distribution_versions(root: Path) -> dict[str, str]:
    config = root / "pyproject.toml"
    own = tomllib.loads(config.read_text()).get("project", {}).get("name") if config.is_file() else None
    # The project's own dev and local version segments change with every commit; its files already key the stats.
    return {
        distribution.metadata["Name"]: distribution.version.partition("+")[0].partition(".dev")[0]
        if distribution.metadata["Name"] == own
        else distribution.version
        for distribution in metadata.distributions()
    }


def reusable_entry(file: Path, opened: bool, check: Callable[[str], list]) -> dict | None:
    entry = json.loads(file.read_text()) if file.is_file() else None
    if entry is None or (
        opened and ("loaded" not in entry or any(check(name) != value for name, value in entry["loaded"].items()))
    ):
        return None
    return entry


def loaded_inputs(
    root: Path,
    static: dict[str, str],
    path: str,
    fresh: list[dict],
    graph: tuple[dict, dict, dict],
    check: Callable[[str], list],
) -> dict[str, list]:
    modules, records, resources = graph
    chosen = [result for result in fresh if path in result.get("files", ())] or fresh
    names = set(modules.values())
    if all("modules" in result for result in chosen):
        collected = {test_file(item) for result in chosen for item in result.get("files", ())} - static.keys()
        names &= {name for result in chosen for name in result["modules"]} - collected
    closure = {}
    for name in names:
        closure.update(test_inputs(root, name, modules, records, resources, False))
    return {name: check(name) for name in closure}


@dataclass
class StatsInputs:
    root: Path
    modules: dict
    resources: dict
    changes: dict
    records: dict = field(default_factory=lambda: {True: {}, False: {}})
    traced: dict = field(default_factory=dict)

    def for_test(self, path: str, covered: list[str] | None, fresh: list[dict] | None = None) -> dict:
        measured = covered is not None
        name = test_file(path)
        group = (name, measured)
        if group not in self.traced:
            self.traced[group] = test_inputs(
                self.root, name, self.modules, self.records[measured], self.resources, not measured
            )
        inputs = dict(self.traced[group])
        if covered == [] and fresh is not None and any(self.records[measured][name][2] for name in inputs):
            check = functools.partial(current_input, self.root, self.changes, {})
            loaded = loaded_inputs(
                self.root, inputs, path, fresh, (self.modules, self.records[measured], self.resources), check
            )
            inputs.update({name: value[0] for name, value in loaded.items()})
        for source in covered or []:
            inputs.update(test_inputs(self.root, source, self.modules, self.records[measured], self.resources, False))
        if not any(not name.startswith("tests/") for name in inputs):
            for source in self.changes.keys() & self.resources:
                inputs.update(
                    test_inputs(self.root, source, self.modules, self.records[measured], self.resources, not measured)
                )
        return inputs


def per_test_stats(path: str, fresh: list[dict], count: int) -> tuple[dict | None, list[str] | None]:
    durations = {
        test: seconds for result in fresh for test, seconds in result["durations"].items() if selects(path, test)
    }
    dependencies = {
        test: paths for result in fresh for test, paths in result.get("dependencies", {}).items() if test in durations
    }
    measured = bool(durations) and all(dependencies.get(test) for test in durations)
    if any("dependencies" in result for result in fresh) and not measured:
        return None, None
    tests = {
        function: sorted({test for result in fresh for test in result["tests"].get(function, []) if test in durations})
        for function in {function for result in fresh for function in result["tests"]}
    }
    result = {
        "status": 0,
        "tests": tests,
        "durations": durations,
        "cpu": sum(result["cpu"] for result in fresh) / count,
    }
    if measured:
        result["dependencies"] = dependencies
    covered = sorted({source for paths in dependencies.values() for source in paths}) if measured else None
    if not measured and any("modules" in result for result in fresh):
        covered = []
    return result, covered


def collect_cached_stats(
    root: Path,
    folder: Path,
    changes: dict,
    files: list[str],
    options: list[str],
    collect: Callable[[list[str]], list[dict]],
) -> list[dict]:
    from scripts.ci_mutation.stats_dependencies import StoredStats

    if base := os.environ.get("MUTATION_STATS_SEED_BASE"):
        from scripts.ci_mutation.stats_seed import seed_once

        seed_once(root, folder, base)
    tracked = subprocess.check_output(TRACKED, cwd=root, text=True).split("\0")
    sources = {
        path: hashlib.sha256((root / path).read_bytes()).hexdigest()
        for path in tracked
        if path and (path in CONFIGURATION or path.startswith("scripts/ci_mutation/")) and (root / path).is_file()
    }
    versions = distribution_versions(root)
    context = dict(zip(CONTEXT_FIELDS, (SCHEMA, sources, options, [sys.version, sys.platform, versions])))
    context["inventory"] = sorted(filter(None, tracked))
    modules = {module_name(Path(path)): path for path in tracked if Path(path).suffix == ".py"}
    folder.mkdir(parents=True, exist_ok=True)
    resources = {}
    for path in tracked:
        if path and (root / path).is_file():
            parts = Path(path).parts
            for index in range(len(parts)):
                resources.setdefault("/".join(parts[index:]), set()).add(path)
    inputs = StatsInputs(root, modules, resources, changes)
    stored = StoredStats(root, folder, context, changes)
    compatible = {path: record for path in files if (record := stored.load(path)) is not None}
    reused = list(compatible.values())
    missing = [path for path in files if path not in compatible]
    report(f"Mutation stats cache: reused {len(reused)} test files; collect {len(missing)}")
    fresh = collect(missing) if missing else []
    if any(result["status"] != 0 for result in fresh):
        raise SystemExit(1)
    for path in missing:
        result, covered = per_test_stats(path, fresh, len(missing))
        if result is not None:
            stored.save(path, result, inputs.for_test(path, covered, fresh), covered is not None)
    if exported := os.environ.get("MUTATION_STATS_EXPORT"):
        shutil.copytree(folder, exported, dirs_exist_ok=True)
    return reused + [
        {name: value for name, value in result.items() if name not in ("files", "modules")} for result in fresh
    ]
