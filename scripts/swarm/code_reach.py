"""Which functions and methods under the packages an entry point reaches, and the Redis commands those send."""

import ast
import builtins
import json
import os
import re
import shutil
import socket
import sqlite3
import subprocess
import threading
import time
from collections.abc import Iterable
from dataclasses import dataclass, field
from functools import cache
from pathlib import Path

import redis
from redis.client import PubSub

from scripts.swarm.redis_wire import HELPERS, lua, methods, wire

PACKAGES = ("scripts", "hooks")
OWN = ("self", "cls")
RAW = "execute_command"
CLIENT_TYPES = frozenset({"Redis", "StrictRedis", "Pipeline"})


@dataclass
class Unit:
    module: str
    owner: str = ""
    names: set = field(default_factory=set)
    dotted: set = field(default_factory=set)
    reads: set = field(default_factory=set)
    own: set = field(default_factory=set)
    typed: set = field(default_factory=set)
    nodes: list = field(default_factory=list)


@cache
def _stdlib() -> frozenset[str]:
    owners = (dict, list, set, str, bytes, Path, os, shutil, json, time, socket.socket, sqlite3.Connection)
    owners += (sqlite3.Cursor, subprocess.Popen, threading.Event, threading.Thread)
    return frozenset().union(*(methods(owner) for owner in owners))


def _module_name(root: Path, path: Path) -> str:
    parts = path.relative_to(root).with_suffix("").parts
    return ".".join(parts[:-1] if parts[-1] == "__init__" else parts)


def parse(root: Path) -> dict:
    return {
        _module_name(root, path): (ast.parse(path.read_bytes()), path.stem == "__init__")
        for package in PACKAGES
        for path in sorted((root / package).rglob("*.py"))
    }


def _dotted(node: ast.expr) -> str:
    if isinstance(node, ast.Attribute) and (base := _dotted(node.value)):
        return f"{base}.{node.attr}"
    return node.id if isinstance(node, ast.Name) else ""


def _tail(node: ast.expr) -> str:
    if isinstance(node, ast.Call):
        return _tail(node.func)
    if isinstance(node, ast.Attribute):
        return node.attr
    return node.id if isinstance(node, ast.Name) else ""


def _walk(body: list):
    for statement in body:
        yield from ast.walk(statement)


def _absolute(module: str, is_package: bool, node: ast.ImportFrom, modules: dict) -> str:
    package = module if is_package else module.rpartition(".")[0]
    if not node.level:
        base = node.module
        sibling = f"{package}.{base}"
        return sibling if sibling.partition(".")[0] in PACKAGES and sibling in modules else base
    parts = package.split(".")
    parts = parts[: len(parts) - node.level + 1]
    return ".".join([*parts, *([node.module] if node.module else [])])


def _bind(module: str, is_package: bool, node: ast.AST, scope: dict, modules: dict) -> None:
    if isinstance(node, ast.Import):
        for alias in node.names:
            target = alias.name if alias.asname else alias.name.split(".")[0]
            scope[alias.asname or target] = [f"@{target}"]
    elif isinstance(node, ast.ImportFrom):
        base = _absolute(module, is_package, node, modules)
        for alias in node.names:
            scope[alias.asname or alias.name] = [f"@{base}.{alias.name}"]


def _assigned(node: ast.AST) -> tuple[str, ast.Call] | None:
    if isinstance(node, ast.Assign):
        target, value = node.targets[0], node.value
    elif isinstance(node, ast.withitem):
        target, value = node.optional_vars, node.context_expr
    else:
        return None
    return (target.id, value) if isinstance(target, ast.Name) and isinstance(value, ast.Call) else None


def _bindings(node: ast.AST) -> tuple[list, ast.expr | None]:
    if isinstance(node, ast.Assign):
        return node.targets, node.value
    if isinstance(node, (ast.AnnAssign, ast.NamedExpr)) and node.value is not None:
        return [node.target], node.value
    if isinstance(node, ast.withitem) and node.optional_vars is not None:
        return [node.optional_vars], node.context_expr
    if isinstance(node, ast.comprehension):
        return [n for n in ast.walk(node.target) if isinstance(n, ast.Name)], ast.Constant(None)
    return [], None


def _pairs(target: ast.expr, value: ast.expr) -> list[tuple[str, ast.expr]]:
    """Each name a binding sets with the value it gets; a name unpacked from a value that is not a literal tuple
    gets no pair, so its type stays unknown."""
    if isinstance(target, ast.Name):
        return [(target.id, value)]
    if not isinstance(target, (ast.Tuple, ast.List)):
        return []
    if isinstance(value, (ast.Tuple, ast.List)) and len(value.elts) == len(target.elts):
        return [pair for t, v in zip(target.elts, value.elts) for pair in _pairs(t, v)]
    return []


def _annotated(node: ast.AST) -> dict[str, bool]:
    """Parameters whose type is known: true when annotated as a Redis client or pipeline, false for *args and
    **kwargs."""
    if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        return {}
    params = [*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs]
    typed = {
        p.arg: True
        for p in params
        if p.annotation and CLIENT_TYPES & set(re.findall(r"\w+", ast.unparse(p.annotation)))
    }
    return typed | {p.arg: False for p in (node.args.vararg, node.args.kwarg) if p}


def _main_guard(node: ast.AST) -> bool:
    return isinstance(node, ast.If) and "__name__" in {n.id for n in ast.walk(node.test) if isinstance(n, ast.Name)}


def _dunder(name: str) -> bool:
    return name.startswith("__") and name.endswith("__")


class Program:
    """Every function, method and module body under the packages, with the names each one reads."""

    def __init__(self, modules: dict):
        self.modules = modules
        self.units: dict[str, Unit] = {}
        self.scopes: dict[str, dict] = {}
        self.members: dict[str, dict[str, str]] = {}
        self.resolving: set[tuple[str, str]] = set()
        for module, (tree, is_package) in modules.items():
            self._index(module, tree, is_package)
        named: dict[str, list[str]] = {}
        for key in self.units:
            named.setdefault(re.split(r"[:.]", key)[-1], []).append(key)
        self.unique = {name: keys[0] for name, keys in named.items() if len(keys) == 1}
        self.scoped: dict[int, dict] = {id(unit): self._nested(unit) for unit in self.units.values()}
        self.clients: set[str] = set()
        self.factories: set[str] = set()
        self._receivers()
        self.typing = {id(unit): self._typed(unit) for unit in self.units.values()}
        self.subscribers = self._subscribers()

    def _add(self, key: str, module: str, body: list, owner: str = "") -> None:
        unit = self.units[key] = Unit(module, owner, nodes=list(_walk(body)))
        bound = {}
        for node in unit.nodes:
            if (found := _assigned(node)) and isinstance(found[1].func, ast.Name):
                bound[found[0]] = found[1].func.id
        for node in unit.nodes:
            if isinstance(node, ast.Name):
                unit.names.add(node.id)
            elif isinstance(node, ast.Attribute):
                self._attribute(unit, node, bound)

    def _attribute(self, unit: Unit, node: ast.Attribute, bound: dict) -> None:
        if dotted := _dotted(node):
            unit.dotted.add(dotted)
        base = _tail(node.value)
        if unit.owner and base in OWN:
            unit.own.add(node.attr)
        elif base in bound:
            unit.typed.add((bound[base], node.attr))
        elif node.attr not in _stdlib():
            unit.reads.add(node.attr)

    def _index(self, module: str, tree: ast.Module, is_package: bool) -> None:
        scope = self.scopes[module] = {}
        loose = []
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                self._add(f"{module}:{node.name}", module, [node])
                scope[node.name] = [f"{module}:{node.name}"]
            elif isinstance(node, ast.ClassDef):
                scope[node.name] = [self._class(module, node)]
            elif not _main_guard(node):
                loose.append(node)
                _bind(module, is_package, node, scope, self.modules)
        self._add(f"{module}:", module, loose)

    def _class(self, module: str, node: ast.ClassDef) -> str:
        owner = f"{module}:{node.name}"
        defs = [n for n in node.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
        self._add(owner, module, [*node.bases, *(n for n in node.body if n not in defs)])
        members = self.members[owner] = {}
        for item in defs:
            members[item.name] = f"{owner}.{item.name}"
            self._add(members[item.name], module, [item], owner)
        return owner

    def resolve(self, module: str, name: str, scope: dict) -> list[str]:
        """The units a name read in a module stands for: a function, a class, or what an import binds it to."""
        if (module, name) in self.resolving:
            return []
        self.resolving.add((module, name))
        found = []
        for target in scope.get(name) or self.scopes[module].get(name, []):
            found.extend(self._import(target[1:]) if target.startswith("@") else [target])
        self.resolving.discard((module, name))
        return found

    def _import(self, dotted: str) -> list[str]:
        if dotted in self.modules:
            return [f"{dotted}:"]
        owner, _, name = dotted.rpartition(".")
        if owner in self.modules:
            return [f"{owner}:", *self.resolve(owner, name, {})]
        return self._import(owner) if owner else []

    def _nested(self, unit: Unit) -> dict:
        scope: dict = {}
        for node in unit.nodes:
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                _bind(unit.module, self.modules[unit.module][1], node, scope, self.modules)
        return scope

    def edges(self, unit: Unit) -> list[str]:
        """The units a unit names directly: its module body, what its names resolve to and typed method calls."""
        scope = self._nested(unit)
        found = [f"{unit.module}:"]
        for name in unit.names:
            found.extend(self.resolve(unit.module, name, scope))
        for name in unit.dotted:
            head, _, rest = name.partition(".")
            for target in scope.get(head) or self.scopes[unit.module].get(head, []):
                found.extend(self._import(f"{target[1:]}.{rest}") if target.startswith("@") else [])
        for constructor, name in unit.typed:
            for owner in self.resolve(unit.module, constructor, scope):
                found.extend([self.members[owner][name]] if name in self.members.get(owner, {}) else [])
        return found + [self.members[unit.owner][name] for name in unit.own if name in self.members[unit.owner]]

    def loose(self, unit: Unit) -> set[str]:
        """Method names a unit reads on objects of no known class, which any reached class may answer."""
        scope = self._nested(unit)
        untyped = {
            name
            for constructor, name in unit.typed
            if name not in _stdlib()
            and not any(owner in self.members for owner in self.resolve(unit.module, constructor, scope))
        }
        inherited = {name for name in unit.own if name not in self.members[unit.owner]} if unit.owner else set()
        return unit.reads | untyped | inherited

    def reach(self, entries: tuple[str, ...]) -> dict[str, str]:
        """Every unit the entries reach, each mapped to the unit that first reached it."""
        if missing := [entry for entry in entries if entry not in self.units]:
            raise KeyError(f"no function {', '.join(missing)} under {', '.join(PACKAGES)}")
        return _Reach(self, entries).run()

    def commands(self, keys: Iterable[str]) -> dict[str, set[str]]:
        found: dict[str, set[str]] = {}
        for key in keys:
            for command in self._issued(self.units[key]):
                found.setdefault(command, set()).add(key)
        return found

    def _issued(self, unit: Unit) -> set[str]:
        sent: set[str] = set()
        local = self.typing[id(unit)]
        for node in unit.nodes:
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                sent |= lua(node.value)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                sent |= self._call(node, unit, local)
        return sent

    def _call(self, node: ast.Call, unit: Unit, local: dict[str, bool]) -> frozenset[str]:
        receiver, name = node.func.value, node.func.attr
        if name == RAW:
            first = node.args[0] if node.args else None
            return frozenset({str(first.value).upper()}) if isinstance(first, ast.Constant) else frozenset()
        client = self._receiver(receiver, unit, local)
        if client and name in HELPERS:
            return HELPERS[name]()
        if client and name in methods(redis.Redis):
            return wire(redis.Redis, name)
        if _tail(receiver) in self.subscribers and name in methods(PubSub):
            return wire(PubSub, name)
        return frozenset()

    def _factory(self, func: ast.expr, unit: Unit, local: dict[str, bool]) -> bool:
        """Whether a call makes a Redis client or pipeline: a redis-py constructor, or a function here that returns
        one."""
        if isinstance(func, ast.Attribute) and func.attr == "pipeline":
            return self._receiver(func.value, unit, local)
        if isinstance(func, ast.Attribute) and func.attr == "from_url":
            return _tail(func.value) in CLIENT_TYPES | {"redis"}
        if _tail(func) in CLIENT_TYPES:
            return True
        return any(key in self.factories for key in self._callees(func, unit) or [])

    def _callees(self, func: ast.expr, unit: Unit) -> list[str] | None:
        """The functions here a call may run, found through the unit's names and imports, its own class, or a name
        only one function defines; None when the callee cannot be found here."""
        scope = self.scoped[id(unit)]
        if isinstance(func, ast.Name):
            bound = func.id in scope or func.id in self.scopes[unit.module] or hasattr(builtins, func.id)
            return self.resolve(unit.module, func.id, scope) if bound else None
        if not isinstance(func, ast.Attribute):
            return None
        if unit.owner and _tail(func.value) in OWN:
            found = self.members[unit.owner].get(func.attr)
            return [found] if found else None
        head, _, rest = _dotted(func).partition(".")
        targets = [t[1:] for t in scope.get(head) or self.scopes[unit.module].get(head, []) if t.startswith("@")]
        if targets:
            return [key for target in targets for key in self._import(f"{target}.{rest}")]
        found = self.unique.get(func.attr)
        return [found] if found else None

    def _held(self, expr: ast.expr, unit: Unit, local: dict[str, bool], loose: bool = False) -> bool:
        """Whether an expression holds a Redis client or pipeline: a constructor or factory call, a name the unit
        binds to one, or an attribute bound to one somewhere. Loose also takes a name the unit does not bind, such
        as a parameter, when that name holds a client somewhere."""
        if isinstance(expr, ast.BoolOp):
            return any(self._held(value, unit, local, loose) for value in expr.values)
        if isinstance(expr, ast.IfExp):
            return self._held(expr.body, unit, local, loose) or self._held(expr.orelse, unit, local, loose)
        if isinstance(expr, ast.Call):
            return self._factory(expr.func, unit, local)
        if isinstance(expr, ast.Name):
            return local[expr.id] if expr.id in local else loose and expr.id in self.clients
        return isinstance(expr, ast.Attribute) and expr.attr in self.clients

    def _receiver(self, expr: ast.expr, unit: Unit, local: dict[str, bool]) -> bool:
        if self._held(expr, unit, local, loose=True):
            return True
        chained = isinstance(expr, ast.Call) and isinstance(expr.func, ast.Attribute)
        return chained and expr.func.attr in methods(redis.Redis) and self._receiver(expr.func.value, unit, local)

    def _typed(self, unit: Unit) -> dict[str, bool]:
        """The names a unit binds, each true when a binding holds a Redis client or pipeline. Bindings are read in
        source order once per binding, so a name bound later in a loop still reaches an alias read earlier. A name bound only to a call whose callee cannot be found here, or
        only as a loop target, gets no entry, so it falls back by name."""
        local = {name: held for node in unit.nodes for name, held in _annotated(node).items()}
        bindings = sorted(
            (found for found in map(_bindings, unit.nodes) if found[0]),
            key=lambda found: (found[0][0].lineno, found[0][0].col_offset),
        )
        pairs = [pair for targets, value in bindings for t in targets for pair in _pairs(t, value)]
        for _ in pairs:
            for target, value in pairs:
                unresolved = isinstance(value, ast.Call) and self._callees(value.func, unit) is None
                if self._held(value, unit, local):
                    local[target] = True
                elif not unresolved:
                    local[target] = local.get(target) is True
        return local

    def _spread(self, key: str, unit: Unit) -> None:
        local = self._typed(unit)
        self.clients |= {name for name, held in local.items() if held}
        for node in unit.nodes:
            targets, value = _bindings(node)
            if value is not None and self._held(value, unit, local):
                self.clients |= {target.attr for target in targets if isinstance(target, ast.Attribute)}
            if isinstance(node, ast.keyword) and node.arg and self._held(node.value, unit, local):
                self.clients.add(node.arg)
            if isinstance(node, ast.Return) and node.value is not None and self._held(node.value, unit, local):
                self.factories.add(key)

    def _receivers(self) -> None:
        """Grow the names holding a Redis client and the functions returning one, typed from the client and
        pipeline constructors and annotations, until neither changes. Passes are capped at one per unit so a run
        always ends."""
        for _ in self.units:
            size = len(self.clients), len(self.factories)
            for key, unit in self.units.items():
                self._spread(key, unit)
            if (len(self.clients), len(self.factories)) == size:
                return

    def _subscribers(self) -> set[str]:
        """Names bound to a pubsub handle."""
        subscribers = set()
        for unit in self.units.values():
            for node in unit.nodes:
                if (found := _assigned(node)) and _tail(found[1].func) == "pubsub":
                    subscribers.add(found[0])
        return subscribers


class _Reach:
    """A method is reached once its class is reached and reached code reads its name, or calls it on self."""

    def __init__(self, program: Program, entries: tuple[str, ...]):
        self.program = program
        self.seen: dict[str, str] = {}
        self.classes: list[str] = []
        self.names: dict[str, str] = {}
        self.pending = [(entry, "") for entry in entries]

    def run(self) -> dict[str, str]:
        while self.pending:
            key, parent = self.pending.pop()
            if key not in self.seen:
                self.seen[key] = parent
                self._visit(key, self.program.units[key])
        return self.seen

    def _visit(self, key: str, unit: Unit) -> None:
        members = self.program.members
        self.pending.extend((found, key) for found in self.program.edges(unit))
        for name in self.program.loose(unit) - self.names.keys():
            self.names[name] = key
            self.pending.extend((members[owner][name], key) for owner in self.classes if name in members[owner])
        if key in members:
            self.classes.append(key)
            for name, member in members[key].items():
                if name in self.names or _dunder(name):
                    self.pending.append((member, self.names.get(name, key)))
