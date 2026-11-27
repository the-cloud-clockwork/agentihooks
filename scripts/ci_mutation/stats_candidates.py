import ast
import inspect
import json
from collections import defaultdict
from collections.abc import Mapping, Sequence
from itertools import islice
from pathlib import Path
from typing import Any

import pytest

type Key = tuple[str, int]


def dotted(node: ast.AST) -> list[str]:
    if isinstance(node, ast.Name):
        return [node.id]
    if isinstance(node, ast.Attribute):
        return [*dotted(node.value), node.attr]
    return []


def initialized(node: ast.AST) -> Sequence[ast.AST]:
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        return [*node.decorator_list, *node.args.defaults, *(value for value in node.args.kw_defaults if value)]
    if isinstance(node, ast.If) and ast.unparse(node.test) in ("__name__ == '__main__'", "'__main__' == __name__"):
        return node.orelse
    return list(ast.iter_child_nodes(node))


def names_in(node: ast.AST, assignments: Mapping[str, ast.AST]) -> set[str] | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return {node.value}
    if isinstance(node, ast.Name) and node.id in assignments:
        return names_in(assignments[node.id], {})
    if isinstance(node, ast.Dict):
        parts = [names_in(value, assignments) for value in node.keys if value]
    elif isinstance(node, (ast.Tuple, ast.List, ast.Set)):
        parts = [names_in(value, assignments) for value in node.elts]
    elif isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr in ("keys", "items"):
        return names_in(node.func.value, assignments)
    else:
        return None
    return (
        {name for part in parts if part is not None for name in part}
        if all(part is not None for part in parts)
        else None
    )


def lookup_names(tree: ast.AST, argument: ast.AST) -> set[str] | None:
    assignments = {
        target.id: node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Assign)
        for target in node.targets
        if isinstance(target, ast.Name)
    }
    if isinstance(argument, ast.Name):
        for node in ast.walk(tree):
            if not isinstance(node, (ast.For, ast.AsyncFor)) or not (
                node.lineno <= argument.lineno <= (node.end_lineno or node.lineno)
            ):
                continue
            target = node.target.elts[0] if isinstance(node.target, (ast.Tuple, ast.List)) else node.target
            if isinstance(target, ast.Name) and target.id == argument.id:
                return names_in(node.iter, assignments)
    return names_in(argument, assignments)


class Reach:
    def __init__(self, root: Path, changes: dict[str, list[int]]) -> None:
        self.root = root.resolve()
        self.nodes: dict[Key, ast.AST] = {}
        self.local: dict[str, dict[str, set[Key]]] = defaultdict(lambda: defaultdict(set))
        self.named: dict[str, set[Key]] = defaultdict(set)
        self.aliases: dict[str, dict[str, set[tuple[str, str]]]] = defaultdict(lambda: defaultdict(set))
        self.modules: dict[str, str] = {}
        self.invalid: set[Key] = set()
        self.index()
        self.wanted = self.targets(changes) | self.invalid
        self.edges: dict[Key, set[Key]] = {}
        for key, node in self.nodes.items():
            edges, unknown = self.references(key[0], node, key[1] == 0)
            self.edges[key] = edges
            if unknown:
                self.wanted.add(key)
        reverse: dict[Key, set[Key]] = defaultdict(set)
        for key, edges in self.edges.items():
            for edge in edges:
                reverse[edge].add(key)
        pending = list(self.wanted)
        for key in islice(pending, len(self.nodes)):
            for parent in reverse[key] - self.wanted:
                self.wanted.add(parent)
                pending.append(parent)

    def index(self) -> None:
        for path in sorted(self.root.rglob("*.py")):
            relative = path.relative_to(self.root)
            if any(
                part.startswith(".") or part in ("mutants", "__pycache__", "node_modules") for part in relative.parts
            ):
                continue
            name = relative.as_posix()
            self.modules[".".join(relative.with_suffix("").parts).removesuffix(".__init__")] = name
            self.read(name)
        for path, tree in list(self.nodes.items()):
            if path[1] == 0:
                self.imports(path[0], tree)
        for path, bindings in list(self.local.items()):
            for name, keys in bindings.items():
                self.named[name].update(keys)
            for name in self.aliases[path]:
                self.named[name].update(self.binding(path, name))

    def read(self, path: str) -> None:
        try:
            tree = ast.parse((self.root / path).read_text())
        except (OSError, SyntaxError, UnicodeError):
            self.invalid.add((path, 0))
            self.nodes[path, 0] = ast.Module(body=[], type_ignores=[])
            return
        self.nodes[path, 0] = tree
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                key = path, node.lineno
                self.nodes[key] = node
                self.local[path][node.name].add(key)
                self.named[node.name].add(key)
        for node in tree.body:
            if isinstance(node, (ast.Assign, ast.AnnAssign)):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                for target in targets:
                    if isinstance(target, ast.Name) and node.value is not None:
                        key = path, node.lineno
                        self.nodes[key] = node.value
                        self.local[path][target.id].add(key)

    def imports(self, path: str, tree: ast.AST) -> None:
        package = path.removesuffix(".py").split("/")[:-1]
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    self.aliases[path][alias.asname or alias.name.split(".")[0]].add(
                        (alias.name if alias.asname else alias.name.split(".")[0], "")
                    )
            elif isinstance(node, ast.ImportFrom):
                parts = package[: len(package) - node.level + 1] if node.level else []
                module = ".".join([*parts, *(node.module.split(".") if node.module else [])])
                for alias in node.names:
                    self.aliases[path][alias.asname or alias.name].add((module, alias.name))

    def module_keys(self, module: str) -> set[Key]:
        return {
            (self.modules[prefix], 0)
            for length in range(1, len(module.split(".")) + 1)
            if (prefix := ".".join(module.split(".")[:length])) in self.modules
        }

    def binding(self, path: str, name: str) -> set[Key]:
        result = set(self.local[path].get(name, ()))
        for module, member in self.aliases[path].get(name, ()):
            result.update(self.module_keys(module))
            if member and module in self.modules:
                result.update(self.local[self.modules[module]].get(member, ()))
                result.update(self.module_keys(f"{module}.{member}"))
        return result

    def attribute(self, path: str, node: ast.Attribute, external: set[str]) -> set[Key]:
        parts = dotted(node)
        if not parts:
            return set(self.named.get(node.attr, ()))
        aliases = self.aliases[path].get(parts[0], ())
        if aliases:
            result: set[Key] = set()
            for module, member in aliases:
                components = [module, *([member] if member else []), *parts[1:]]
                for length in range(1, len(components)):
                    prefix = ".".join(components[:length])
                    if prefix in self.modules:
                        result.update(self.local[self.modules[prefix]].get(parts[-1], ()))
                result.update(self.module_keys(".".join(components)))
            return result
        return set() if parts[0] in external else set(self.named.get(node.attr, ()))

    def executable_lookup(self, tree: ast.AST, node: ast.Call) -> tuple[set[Key], bool]:
        names = dotted(node.func)
        if not names:
            return set(), False
        name = names[-1]
        if name in ("eval", "exec", "globals", "locals"):
            return set(), True
        position = 1 if name in ("getattr", "hasattr") else 0
        if name not in ("getattr", "hasattr", "getfixturevalue", "import_module", "__import__"):
            return set(), False
        if len(node.args) <= position:
            return set(), True
        values = lookup_names(tree, node.args[position])
        if values is None or any(value.startswith(".") for value in values):
            return set(), True
        result: set[Key] = set()
        for value in values:
            if name in ("import_module", "__import__"):
                result.update(self.module_keys(value))
            else:
                result.update(self.named.get(value, ()))
        return result, False

    def references(self, path: str, tree: ast.AST, importing: bool) -> tuple[set[Key], bool]:
        pending = [tree]
        nodes: list[ast.AST] = []
        for node in islice(pending, sum(1 for _ in ast.walk(tree))):
            nodes.append(node)
            pending.extend(initialized(node) if importing else ast.iter_child_nodes(node))
        external = {
            node.arg
            for node in nodes
            if isinstance(node, ast.arg) and node.annotation is not None
            for base in dotted(node.annotation)[:1]
            if self.aliases[path].get(base) and not self.binding(path, base)
        }
        result: set[Key] = set()
        unknown = False
        for node in nodes:
            if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
                result.update(self.binding(path, node.id))
            elif isinstance(node, ast.Attribute) and isinstance(node.ctx, ast.Load):
                result.update(self.attribute(path, node, external))
            elif isinstance(node, ast.Call):
                edges, unresolved = self.executable_lookup(tree, node)
                result.update(edges)
                unknown |= unresolved
            elif isinstance(node, (ast.Import, ast.ImportFrom)):
                for alias in node.names:
                    for module, _ in self.aliases[path].get(alias.asname or alias.name.split(".")[0], ()):
                        result.update(self.module_keys(module))
        return result, unknown

    def targets(self, changes: dict[str, list[int]]) -> set[Key]:
        result: set[Key] = set()
        for path, lines in changes.items():
            matched = {
                key
                for key, node in self.nodes.items()
                if key[0] == path
                and isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
                and any(node.lineno <= line <= (node.end_lineno or node.lineno) for line in lines)
            }
            result.update(matched or {(path, 0)})
        return result

    def function_keys(self, function: Any) -> set[Key] | None:
        try:
            function = inspect.unwrap(function)
            source = inspect.getsourcefile(function)
            if source is None:
                return None
            path = Path(source).resolve()
            if not path.is_relative_to(self.root):
                return set()
            parts = path.relative_to(self.root).parts
            relative = Path(*(parts[1:] if parts[0] == "mutants" else parts)).as_posix()
            code = function.__code__
        except (OSError, TypeError, ValueError, AttributeError):
            return None
        result = {
            key
            for key in self.local[relative].get(code.co_name, ())
            if isinstance(node := self.nodes[key], (ast.FunctionDef, ast.AsyncFunctionDef))
            and min([node.lineno, *(decorator.lineno for decorator in node.decorator_list)])
            <= code.co_firstlineno
            <= (node.end_lineno or node.lineno)
        }
        return result or None

    def reaches(self, function: Any) -> bool:
        keys = self.function_keys(function)
        return keys is None or bool(keys & self.wanted)

    def keep(self, item: pytest.Item) -> bool:
        if not isinstance(item, pytest.Function) or item.cls is not None:
            return True
        if self.reaches(item.obj):
            return True
        info: Any = getattr(item, "_fixtureinfo", None)
        if info is None:
            return True
        for name in item.fixturenames:
            for fixture in info.name2fixturedefs.get(name, ()):
                if self.reaches(fixture.func):
                    return True
        module = self.function_keys(item.obj)
        return module is None or any((path, 0) in self.wanted for path, _ in module)


def candidate_tests(items: list[pytest.Item], root: Path, changes: dict[str, list[int]]) -> list[pytest.Item]:
    if not items or not changes:
        return items
    reach = Reach(root, changes)
    config: Any = getattr(items[0], "config", None)
    if config is not None:
        for plugin in config.pluginmanager.get_plugins():
            for name in dir(plugin):
                if name.startswith("pytest_runtest_") or name in (
                    "pytest_fixture_setup",
                    "pytest_fixture_post_finalizer",
                ):
                    function = getattr(plugin, name)
                    if (inspect.isfunction(function) or inspect.ismethod(function)) and reach.reaches(function):
                        return items
    return [item for item in items if reach.keep(item)]


def pytest_addoption(parser: Any) -> None:
    parser.addoption("--mutation-stats-scope", default="")


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    option = str(config.getoption("mutation_stats_scope"))
    if not option:
        return
    path = Path(option)
    changes = json.loads(path.read_text())
    selected = candidate_tests(items, path.parent, {name: change["lines"] for name, change in changes.items()})
    removed = [item for item in items if item not in selected]
    print(f"Mutation stats candidates: kept {len(selected)} of {len(items)}", flush=True)
    items[:] = selected
    if removed:
        config.hook.pytest_deselected(items=removed)
