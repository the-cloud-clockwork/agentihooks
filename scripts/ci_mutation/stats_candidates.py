import ast
import inspect
import json
from collections import defaultdict
from collections.abc import Mapping, Sequence
from itertools import islice
from pathlib import Path
from typing import Any, cast

import pytest

type Key = tuple[str, int]
EXTERNAL: Key = ("", -1)
UNKNOWN: Key = ("", -2)

__all__ = ["candidate_tests", "pytest_addoption", "pytest_collection_modifyitems"]

LOOKUPS = frozenset(
    ("getattr", "hasattr", "getfixturevalue", "import_module", "__import__", "eval", "exec", "globals", "locals")
)


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
    assignments: dict[str, ast.expr] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    prior = assignments.get(target.id)
                    assignments[target.id] = (
                        ast.Tuple(elts=[prior, node.value], ctx=ast.Load()) if prior is not None else node.value
                    )
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
        self.changes = changes
        self.nodes: dict[Key, ast.AST] = {}
        self.parents: dict[Key, Key] = {}
        self.bindings: dict[Key, dict[str, list[ast.AST]]] = {}
        self.aliases: dict[Key, dict[str, list[tuple[str, str]]]] = {}
        self.edges: dict[Key, set[Key]] = {}
        self.wanted: set[Key] = set()
        self.active: set[tuple[Key, str]] = set()
        self.unknown: set[Key] = set()
        self.invalid: set[Key] = set()
        self.arguments: dict[Key, dict[str, set[Key]]] = defaultdict(dict)
        self.dirty: set[Key] = set()
        self.fields: dict[Key, dict[str, list[tuple[Key, ast.AST]]]] = defaultdict(lambda: defaultdict(list))

    def module_path(self, module: str) -> str | None:
        parts = module.split(".")
        for base in (self.root, self.root / "scripts"):
            for path in (base.joinpath(*parts).with_suffix(".py"), base.joinpath(*parts, "__init__.py")):
                if path.is_file():
                    return path.relative_to(self.root).as_posix()
        return None

    def read(self, path: str) -> Key:
        key = path, 0
        if key in self.nodes:
            return key
        try:
            tree = ast.parse((self.root / path).read_text())
        except (OSError, SyntaxError, UnicodeError):
            tree = ast.Module(body=[], type_ignores=[])
            self.unknown.add(key)
            self.invalid.add(key)
        self.nodes[key] = tree
        self.bindings[key] = {}
        self.aliases[key] = {}
        self.scan(key, tree.body)
        return key

    def scan(self, scope: Key, statements: list[ast.stmt]) -> None:
        for node in statements:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                key = scope[0], node.lineno
                self.nodes[key] = node
                self.parents[key] = scope
                self.bindings[key] = {}
                self.aliases[key] = {}
                self.bindings[scope].setdefault(node.name, []).append(node)
                self.scan(key, node.body)
            elif isinstance(node, (ast.Import, ast.ImportFrom)):
                self.imports(scope, node)
            else:
                self.assignment(scope, node)
                for field in ("body", "orelse", "finalbody"):
                    children = getattr(node, field, None)
                    if isinstance(children, list):
                        self.scan(scope, cast(list[ast.stmt], children))
                if isinstance(node, ast.Try):
                    for handler in node.handlers:
                        self.scan(scope, handler.body)

    def assignment(self, scope: Key, node: ast.AST) -> None:
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            value = node.value
        elif isinstance(node, (ast.For, ast.AsyncFor)):
            targets, value = [node.target], node.iter
        elif isinstance(node, (ast.With, ast.AsyncWith)):
            for item in node.items:
                if isinstance(item.optional_vars, ast.Name):
                    self.bindings[scope].setdefault(item.optional_vars.id, []).append(item.context_expr)
            return
        else:
            return
        if value is not None:
            for target in targets:
                self.bind_target(scope, target, value)

    def bind_target(self, scope: Key, target: ast.AST, value: ast.AST) -> None:
        if isinstance(target, ast.Name):
            self.bindings[scope].setdefault(target.id, []).append(value)
        elif isinstance(target, (ast.Tuple, ast.List)):
            for item in target.elts:
                self.bind_target(scope, item, value)
        elif isinstance(target, ast.Attribute) and isinstance(target.value, ast.Name):
            owner = self.parents.get(scope)
            if owner is not None and isinstance(self.nodes[owner], ast.ClassDef) and target.value.id in ("self", "cls"):
                self.fields[owner][target.attr].append((scope, value))

    def imports(self, scope: Key, node: ast.Import | ast.ImportFrom) -> None:
        package = scope[0].removesuffix(".py").split("/")[:-1]
        if isinstance(node, ast.Import):
            for alias in node.names:
                module = alias.name if alias.asname else alias.name.split(".")[0]
                self.aliases[scope].setdefault(alias.asname or module, []).append((module, ""))
        else:
            parts = package[: len(package) - node.level + 1] if node.level else []
            module = ".".join([*parts, *(node.module.split(".") if node.module else [])])
            for alias in node.names:
                self.aliases[scope].setdefault(alias.asname or alias.name, []).append((module, alias.name))

    def imported(self, module: str, member: str) -> set[Key]:
        path = self.module_path(module)
        if member and (child := self.module_path(f"{module}.{member}")) is not None:
            return {self.read(child)}
        if path is None:
            return {EXTERNAL}
        key = self.read(path)
        return self.binding(key, member) if member else {key}

    def binding(self, scope: Key, name: str) -> set[Key]:
        token = scope, name
        if token in self.active:
            return self.parameter(scope, name)
        self.active.add(token)
        result: set[Key] = set()
        if name in self.bindings[scope]:
            for node in self.bindings[scope][name]:
                result.update(self.value(scope, node))
        elif name in self.aliases[scope]:
            for module, member in self.aliases[scope][name]:
                result.update(self.imported(module, member))
        else:
            result.update(self.parameter(scope, name))
        self.active.remove(token)
        return result

    def parameter(self, scope: Key, name: str) -> set[Key]:
        if name in self.arguments[scope]:
            return self.arguments[scope][name]
        node = self.nodes[scope]
        owner = self.parents.get(scope)
        if name in ("self", "cls") and owner is not None and isinstance(self.nodes[owner], ast.ClassDef):
            return {owner}
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            arguments = [*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs]
            arguments.extend(value for value in (node.args.vararg, node.args.kwarg) if value is not None)
            for argument in arguments:
                if argument.arg == name:
                    if argument.annotation is not None:
                        return self.annotation(scope, argument.annotation)
                    return {EXTERNAL if name == "request" or node.name.startswith("pytest_") else UNKNOWN}
        if scope in self.parents:
            return self.binding(self.parents[scope], name)
        if name in vars(__import__("builtins")) or name in ("__name__", "__file__", "__package__"):
            return {EXTERNAL}
        hook = self.bindings[scope].get("__getattr__", ())
        return {(scope[0], cast(Any, value).lineno) for value in hook} or {UNKNOWN}

    def annotation(self, scope: Key, node: ast.AST) -> set[Key]:
        if isinstance(node, ast.Subscript):
            return self.annotation(scope, node.value)
        if isinstance(node, ast.BinOp):
            return self.annotation(scope, node.left) | self.annotation(scope, node.right)
        if isinstance(node, ast.Constant):
            return {EXTERNAL} if node.value is None else {UNKNOWN}
        if isinstance(node, ast.Name) and node.id in ("Any", "object"):
            return {UNKNOWN}
        return self.value(self.parents.get(scope, scope), node)

    def value(self, scope: Key, node: ast.AST) -> set[Key]:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            return {(scope[0], node.lineno)}
        if isinstance(node, ast.Name):
            return self.binding(scope, node.id)
        if isinstance(node, ast.Attribute):
            return self.attribute(self.value(scope, node.value), node.attr)
        if isinstance(node, ast.Call):
            return self.call_value(scope, node)
        if isinstance(node, ast.IfExp):
            return self.value(scope, node.body) | self.value(scope, node.orelse)
        if isinstance(node, (ast.List, ast.Tuple, ast.Set)):
            return set[Key]().union(*(self.value(scope, value) for value in node.elts))
        if isinstance(node, ast.Subscript):
            return self.subscript(scope, node)
        if isinstance(node, (ast.Constant, ast.BinOp, ast.JoinedStr, ast.Dict, ast.Compare, ast.UnaryOp)):
            return {EXTERNAL}
        if isinstance(node, ast.Lambda):
            return {UNKNOWN}
        return {UNKNOWN}

    def attribute(self, owners: set[Key], name: str) -> set[Key]:
        result: set[Key] = set()
        for owner in owners:
            if owner[0] and name in ("__name__", "__qualname__", "__module__", "__doc__", "__file__", "__package__"):
                result.add(EXTERNAL)
            elif not owner[0]:
                result.add(owner)
            elif owner[1] == 0 or isinstance(self.nodes[owner], ast.ClassDef):
                members = self.bindings[owner].get(name)
                if name in self.fields[owner]:
                    result.update(self.field_value(owner, name))
                if members:
                    for member in members:
                        result.update(self.value(owner, member))
                elif name in self.aliases[owner]:
                    result.update(self.binding(owner, name))
                elif (
                    name not in self.fields[owner]
                    or isinstance(self.nodes[owner], ast.ClassDef)
                    and cast(ast.ClassDef, self.nodes[owner]).bases
                ):
                    result.update(self.missing_attribute(owner, name))
            else:
                result.add(UNKNOWN)
        return result

    def field_value(self, owner: Key, name: str) -> set[Key]:
        token = owner, f"<field:{name}>"
        if token in self.active:
            return {UNKNOWN}
        self.active.add(token)
        result = set[Key]().union(*(self.value(scope, value) for scope, value in self.fields[owner][name]))
        self.active.remove(token)
        return result

    def missing_attribute(self, owner: Key, name: str) -> set[Key]:
        node = self.nodes[owner]
        if isinstance(node, ast.ClassDef):
            bases = set[Key]().union(*(self.value(self.parents[owner], base) for base in node.bases))
            return self.attribute(bases, name) if bases else {UNKNOWN}
        exports = self.bindings[owner].get("__getattr__", ())
        return {(owner[0], cast(Any, value).lineno) for value in exports} or {UNKNOWN}

    def call_value(self, scope: Key, node: ast.Call) -> set[Key]:
        kind = self.lookup_kind(scope, node.func)
        if kind in ("getattr", "hasattr"):
            if len(node.args) < 2:
                return {UNKNOWN}
            owners = self.value(scope, node.args[0])
            names = lookup_names(self.nodes[scope], node.args[1])
            if names is None:
                return {UNKNOWN}
            return set[Key]().union(*(self.attribute(owners, name) for name in names))
        if kind in ("import_module", "__import__"):
            names = lookup_names(self.nodes[scope], node.args[0]) if node.args else None
            return set[Key]().union(*(self.imported(name, "") for name in names)) if names else {UNKNOWN}
        if kind:
            return {UNKNOWN}
        functions = self.value(scope, node.func)
        result: set[Key] = set()
        for key in functions:
            if not key[0] or isinstance(self.nodes[key], ast.ClassDef):
                result.add(key)
            else:
                result.update(self.returns(key))
        return result

    def returns(self, key: Key) -> set[Key]:
        token = key, "<return>"
        if token in self.active:
            return {UNKNOWN}
        self.active.add(token)
        values = [
            node.value
            for node in self.executed(key)
            if isinstance(node, (ast.Return, ast.Yield)) and node.value is not None
        ]
        result = set[Key]().union(*(self.value(key, value) for value in values)) if values else {EXTERNAL}
        self.active.remove(token)
        return result

    def lookup_kind(self, scope: Key, node: ast.AST) -> str:
        parts = dotted(node)
        if not parts:
            return ""
        if len(parts) == 1:
            current = scope
            while current in self.parents and parts[0] not in self.aliases[current]:
                current = self.parents[current]
            kinds = {member for _, member in self.aliases[current].get(parts[0], ()) if member in LOOKUPS}
            if kinds:
                return next(iter(kinds))
        return parts[-1] if parts[-1] in LOOKUPS else ""

    def subscript(self, scope: Key, node: ast.Subscript) -> set[Key]:
        if isinstance(node.value, ast.Call) and dotted(node.value.func) == ["globals"]:
            module = scope[0], 0
            names = lookup_names(self.nodes[scope], node.slice)
            if names is not None:
                return set[Key]().union(*(self.binding(module, name) for name in names))
            if isinstance(node.slice, ast.JoinedStr) and isinstance(node.slice.values[0], ast.Constant):
                prefix = str(node.slice.values[0].value)
                names = {name for name in self.bindings[module] if name.startswith(prefix)}
                return set[Key]().union(*(self.binding(module, name) for name in names))
            return {UNKNOWN}
        owners = self.value(scope, node.value)
        return owners if all(not key[0] for key in owners) else {UNKNOWN}

    def executed(self, key: Key) -> list[ast.AST]:
        root = self.nodes[key]
        pending = list(ast.iter_child_nodes(root)) if key[1] else [root]
        result: list[ast.AST] = []
        for node in islice(pending, sum(1 for _ in ast.walk(root))):
            result.append(node)
            pending.extend(initialized(node))
        return result

    def module_keys(self, module: str) -> set[Key]:
        result: set[Key] = set()
        for length in range(1, len(module.split(".")) + 1):
            path = self.module_path(".".join(module.split(".")[:length]))
            if path is not None:
                result.add(self.read(path))
        return result

    def import_edges(self, key: Key, node: ast.Import | ast.ImportFrom) -> set[Key]:
        result: set[Key] = set()
        for alias in node.names:
            if isinstance(node, ast.Import):
                result.update(self.module_keys(alias.name))
            name = alias.asname or alias.name.split(".")[0]
            for module, member in self.aliases[key].get(name, ()):
                result.update(self.module_keys(module))
                if member:
                    result.update(self.module_keys(f"{module}.{member}"))
                    path = self.module_path(module)
                    if path is not None and member not in self.bindings[path, 0]:
                        result.update(self.bindings_to_keys((path, 0), "__getattr__"))
        return result

    def bindings_to_keys(self, key: Key, name: str) -> set[Key]:
        return {(key[0], cast(Any, node).lineno) for node in self.bindings[key].get(name, ())}

    def references(self, key: Key) -> set[Key]:
        result: set[Key] = set()
        if isinstance(self.nodes[key], ast.ClassDef):
            for name in self.bindings[key]:
                if name.startswith("__") and name.endswith("__"):
                    result.update(self.bindings_to_keys(key, name))
        nodes = self.executed(key)
        called = {id(node.func) for node in nodes if isinstance(node, ast.Call)}
        for node in nodes:
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                result.update(self.import_edges(key, node))
            elif isinstance(node, ast.Call):
                result.update(self.call_edges(key, node))
            elif isinstance(node, (ast.Name, ast.Attribute)) and isinstance(node.ctx, ast.Load):
                values = self.value(key, node)
                if isinstance(node, ast.Attribute) and UNKNOWN in values:
                    result.add(UNKNOWN)
                result.update(value for value in values if value[0] and value[1] != 0)
                if id(node) not in called and self.lookup_kind(key, node) in (
                    "getfixturevalue",
                    "import_module",
                    "__import__",
                    "eval",
                    "exec",
                ):
                    result.add(UNKNOWN)
        return result

    def call_edges(self, key: Key, node: ast.Call) -> set[Key]:
        kind = self.lookup_kind(key, node.func)
        if kind == "globals":
            return set()
        if kind in ("getattr", "hasattr"):
            if not node.args:
                return {UNKNOWN}
            owners = self.value(key, node.args[0])
            result = {owner for owner in owners if not owner[0]}
            for owner in owners:
                if owner[0]:
                    result.update(self.bindings_to_keys(owner, "__getattr__"))
            return result
        if kind in ("import_module", "__import__"):
            return self.call_value(key, node)
        if kind:
            return {UNKNOWN}
        return self.callables(key, node) | self.patch_imports(key, node)

    def callables(self, key: Key, node: ast.Call) -> set[Key]:
        functions = self.value(key, node.func)
        for function in list(functions):
            if function[0] and isinstance(self.nodes[function], ast.ClassDef):
                functions.update(self.bindings_to_keys(function, "__init__"))
                functions.update(self.bindings_to_keys(function, "__new__"))
        for function in functions:
            if function[0] and isinstance(definition := self.nodes[function], (ast.FunctionDef, ast.AsyncFunctionDef)):
                if definition.name not in ("__init__", "__new__"):
                    self.associate(key, function, node)
        return functions

    def patch_imports(self, key: Key, node: ast.Call) -> set[Key]:
        parts = dotted(node.func)
        if not parts or parts[-1] not in ("setattr", "delattr", "patch") or not node.args:
            return set()
        argument = node.args[0]
        names = lookup_names(self.nodes[key], argument)
        if isinstance(argument, ast.JoinedStr) and isinstance(argument.values[0], ast.Constant):
            names = {str(argument.values[0].value).rsplit(".", 1)[0] + ".member"}
        if names is None:
            return set() if not isinstance(argument, (ast.Constant, ast.JoinedStr)) else {UNKNOWN}
        result: set[Key] = set()
        for name in names:
            if "." in name:
                result.update(self.module_keys(name.rsplit(".", 1)[0]))
        return result

    def associate(self, caller: Key, function: Key, call: ast.Call) -> None:
        definition = self.nodes[function]
        if not isinstance(definition, (ast.FunctionDef, ast.AsyncFunctionDef)):
            return
        parameters = [*definition.args.posonlyargs, *definition.args.args]
        values = [self.value(caller, value) for value in call.args]
        if isinstance(call.func, ast.Attribute):
            owners = self.value(caller, call.func.value)
            if any(owner[0] and isinstance(self.nodes[owner], ast.ClassDef) for owner in owners):
                values.insert(0, owners)
        bound = {parameter.arg: value for parameter, value in zip(parameters, values)}
        bound.update({value.arg: self.value(caller, value.value) for value in call.keywords if value.arg})
        for name, value in bound.items():
            previous = self.arguments[function].get(name, set())
            if not value <= previous:
                self.arguments[function][name] = previous | value
                self.dirty.add(function)

    def fill(self, keys: set[Key]) -> None:
        pending = list(keys)
        changed = False
        passes = 0
        while pending or self.dirty:
            if not pending:
                pending.extend(self.dirty)
            key = pending.pop()
            if not key[0] or key in self.edges and key not in self.dirty:
                continue
            self.dirty.discard(key)
            passes += 1
            if passes >= 20000:
                self.unknown.update(pending)
                self.unknown.update(self.dirty)
                self.unknown.add(key)
                self.dirty.clear()
                break
            if key not in self.invalid:
                self.unknown.discard(key)
            edges = self.references(key)
            self.edges[key] = {edge for edge in edges if edge[0]}
            if UNKNOWN in edges:
                self.unknown.add(key)
            pending.extend(self.edges[key] - self.edges.keys())
            changed = True
        if changed:
            self.propagate()

    def propagate(self) -> None:
        self.wanted = set(self.unknown)
        reverse: dict[Key, set[Key]] = defaultdict(set)
        for key, edges in self.edges.items():
            node = cast(Any, self.nodes[key])
            if (
                any(node.lineno <= line <= (node.end_lineno or node.lineno) for line in self.changes.get(key[0], ()))
                if key[1]
                else bool(self.changes.get(key[0]))
                and not any(
                    isinstance(value, (ast.FunctionDef, ast.AsyncFunctionDef))
                    and any(
                        value.lineno <= line <= (value.end_lineno or value.lineno)
                        for line in self.changes.get(key[0], ())
                    )
                    for value in ast.walk(node)
                )
            ):
                self.wanted.add(key)
            for edge in edges:
                reverse[edge].add(key)
        pending = list(self.wanted)
        for key in islice(pending, len(self.nodes)):
            for parent in reverse[key] - self.wanted:
                self.wanted.add(parent)
                pending.append(parent)

    def function_keys(self, function: Any) -> set[Key] | None:
        owner = getattr(function, "__self__", None)
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
        self.read(relative)
        result = {
            key
            for key, node in self.nodes.items()
            if key[0] == relative
            and isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name == code.co_name
            and min([node.lineno, *(decorator.lineno for decorator in node.decorator_list)])
            <= code.co_firstlineno
            <= (node.end_lineno or node.lineno)
        }
        if owner is not None:
            origins = self.class_keys(owner)
            for key in result:
                definition = self.nodes[key]
                if isinstance(definition, (ast.FunctionDef, ast.AsyncFunctionDef)) and definition.args.args:
                    name = definition.args.args[0].arg
                    previous = self.arguments[key].get(name, set())
                    if not origins <= previous:
                        self.arguments[key][name] = previous | origins
                        self.dirty.add(key)
        return result or None

    def class_keys(self, owner: Any) -> set[Key]:
        kind = cast(type[Any], owner if inspect.isclass(owner) else type(owner))
        try:
            source = inspect.getsourcefile(kind)
        except TypeError:
            return {UNKNOWN}
        if source is None:
            return {UNKNOWN}
        path = Path(source).resolve()
        if not path.is_relative_to(self.root):
            return {UNKNOWN}
        parts = path.relative_to(self.root).parts
        relative = Path(*(parts[1:] if parts[0] == "mutants" else parts)).as_posix()
        self.read(relative)
        return {
            key
            for key, node in self.nodes.items()
            if key[0] == relative and isinstance(node, ast.ClassDef) and node.name == kind.__name__
        } or {UNKNOWN}

    def reaches(self, function: Any) -> bool:
        keys = self.function_keys(function)
        if keys is None:
            return True
        self.fill(keys)
        return bool(keys & self.wanted)

    def keep(self, item: pytest.Item) -> bool:
        if not isinstance(item, pytest.Function) or item.cls is not None:
            return True
        if self.reaches(item.obj):
            return True
        for name in ("setup_module", "teardown_module", "setup_function", "teardown_function", "__getattr__"):
            handler = inspect.getattr_static(item.module, name, None)
            if handler is not None and self.reaches(handler):
                return True
        info: Any = getattr(item, "_fixtureinfo", None)
        if info is None:
            return True
        for name in item.fixturenames:
            for fixture in info.name2fixturedefs.get(name, ()):
                if self.reaches(fixture.func):
                    return True
        module = self.function_keys(item.obj)
        keys: set[Key] = {(path, 0) for path, _ in module} if module is not None else set()
        self.fill(keys)
        return module is None or bool(keys & self.wanted)


def candidate_tests(items: list[pytest.Item], root: Path, changes: dict[str, list[int]]) -> list[pytest.Item]:
    if not items or not changes:
        return items
    reach = Reach(root, changes)
    config: Any = getattr(items[0], "config", None)
    if config is not None:
        hooks = cast(dict[str, Any], vars(config.hook))
        for name, hook in hooks.items():
            if name.startswith("pytest_runtest") or name in (
                "pytest_pyfunc_call",
                "pytest_assertion_pass",
                "pytest_fixture_setup",
                "pytest_fixture_post_finalizer",
                "pytest_exception_interact",
                "pytest_report_teststatus",
                "pytest_warning_recorded",
            ):
                implementations: list[Any] = hook.get_hookimpls()
                for implementation in implementations:
                    if reach.reaches(implementation.function):
                        print(
                            f"Mutation stats candidates retained for hook {implementation.function.__qualname__}",
                            flush=True,
                        )
                        return items
    for item in items:
        reach.keep(item)
    return [item for item in items if reach.keep(item)]


def pytest_addoption(parser: Any) -> None:
    parser.addoption("--mutation-stats-scope", default="")


@pytest.hookimpl(trylast=True)
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
