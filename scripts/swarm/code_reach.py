"""Which functions and methods under the packages an entry point reaches, and the Redis commands those send."""

import ast
import json
import os
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
        self.defined: set[str] = set()
        self.resolving: set[tuple[str, str]] = set()
        for module, (tree, is_package) in modules.items():
            self._index(module, tree, is_package)
        self.clients, self.subscribers = self._receivers()

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
                self.defined.add(node.name)
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
            self.defined.add(item.name)
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
        for node in unit.nodes:
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                sent |= lua(node.value)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                sent |= self._call(node)
        return sent

    def _call(self, node: ast.Call) -> frozenset[str]:
        receiver, name = _tail(node.func.value), node.func.attr
        if name == RAW:
            first = node.args[0] if node.args else None
            return frozenset({str(first.value).upper()}) if isinstance(first, ast.Constant) else frozenset()
        if receiver in self.clients and name in HELPERS:
            return HELPERS[name]()
        if receiver in self.clients and name in methods(redis.Redis):
            return wire(redis.Redis, name)
        if receiver in self.subscribers and name in methods(PubSub):
            return wire(PubSub, name)
        return frozenset()

    def _receivers(self) -> tuple[set[str], set[str]]:
        """Names holding a Redis client, which call a command no function here or in the standard library defines,
        and names bound to a pubsub handle."""
        common = _stdlib() | self.defined | {RAW}
        clients, subscribers = set(), set()
        for unit in self.units.values():
            for node in unit.nodes:
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                    name, receiver = node.func.attr, _tail(node.func.value)
                    if receiver and name not in common and name in methods(redis.Redis) and wire(redis.Redis, name):
                        clients.add(receiver)
                elif (found := _assigned(node)) and _tail(found[1].func) == "pubsub":
                    subscribers.add(found[0])
        return clients, subscribers


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
