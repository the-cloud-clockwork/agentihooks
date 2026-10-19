"""The Redis commands each swarm consumer reaches from its entry point, read from the code, and the scheduler keys it
writes, so a deployment can check that the Redis user serving each consumer grants every one of them."""

import argparse
import ast
import inspect
import itertools
import json
import os
import shutil
import socket
import sqlite3
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from functools import cache
from pathlib import Path
from unittest import mock

import redis
from redis.client import Pipeline, PubSub
from redis.lock import Lock

ROOT = Path(__file__).resolve().parents[2]
PACKAGES = ("scripts", "hooks")
LUA = "redis.call("
SAMPLES = ("k", 1, ["k"], {"k": "v"})
SPARE = 3
SCHEDULER_KEYS = {"scheduler entries": "entries", "scheduler wake": "wake:probe"}


@dataclass(frozen=True)
class Consumer:
    role: str
    entries: tuple[str, ...]


CONSUMERS = {
    "controller": Consumer("controller", ("scripts.swarm.controller:main",)),
    "tick": Consumer("controller", ("scripts.swarm.scheduled_tick:main", "scripts.swarm.tick:main")),
    "ledger server": Consumer("ledger", ("scripts.swarm_ledger:run", "scripts.swarm_ledger.ledger_server:main")),
    "worker": Consumer("worker", ("scripts.swarm_v2.worker.start:main",)),
    "hooks": Consumer("worker", ("hooks.hook_manager:main",)),
}


@dataclass
class Unit:
    module: str
    body: list
    owner: str = ""
    names: set = field(default_factory=set)
    dotted: set = field(default_factory=set)
    methods: set = field(default_factory=set)
    own: set = field(default_factory=set)
    typed: set = field(default_factory=set)


class _Recorder(redis.Redis):
    def __init__(self):
        super().__init__()
        self.sent: list[str] = []

    def execute_command(self, *args, **options):
        name = str(args[0]).upper()
        self.sent.append(name)
        if name.endswith("SCAN"):
            return 0, []
        return 1 if name in ("SET", "EVALSHA") else None


def lua(text: str) -> set[str]:
    """The commands a Lua script in the text calls through redis.call."""
    found = set()
    for part in text.split(LUA)[1:]:
        quoted = part.lstrip()
        if quoted[:1] in ("'", '"') and (end := quoted.find(quoted[0], 1)) > 1:
            found.add(quoted[1:end].upper())
    return found


def _target(owner: type, recorder: redis.Redis):
    if owner is Pipeline:
        return recorder.pipeline(transaction=False)
    return recorder.pubsub() if owner is PubSub else recorder


def _record(owner: type, name: str, values: tuple) -> list[str]:
    recorder = _Recorder()
    target = _target(owner, recorder)
    try:
        result = getattr(target, name)(*values)
        if inspect.isgenerator(result):
            list(result)
    except Exception:  # noqa: BLE001 - a refused sample tries the next one
        pass
    if owner is Pipeline:
        recorder.sent.extend(str(args[0]).upper() for args, _ in target.command_stack)
    return recorder.sent


@cache
def wire(owner: type, name: str) -> frozenset[str]:
    """The commands a redis-py method sends, found by calling it on a recording client with sample arguments."""
    params = list(inspect.signature(getattr(owner, name)).parameters.values())[1:]
    params = [p for p in params if p.kind is p.POSITIONAL_OR_KEYWORD]
    required = sum(1 for p in params if p.default is p.empty)
    for width in range(required, min(len(params), required + SPARE) + 1):
        for values in itertools.product(SAMPLES, repeat=width):
            if sent := _record(owner, name, values):
                return frozenset(sent)
    return frozenset()


@cache
def _lock() -> frozenset[str]:
    recorder = _Recorder()
    lock = recorder.lock("k", timeout=1)
    lock.acquire(blocking=False)
    lock.extend(1)
    lock.reacquire()
    lock.release()
    scripts = [value for name, value in vars(Lock).items() if name.startswith("LUA_")]
    return frozenset(recorder.sent).union({"SCRIPT LOAD"}, *map(lua, scripts))


HELPERS = {
    "lock": _lock,
    "register_script": lambda: frozenset({"EVALSHA", "SCRIPT LOAD"}),
    "transaction": lambda: frozenset({"WATCH", "MULTI", "EXEC"}),
    "execute": lambda: frozenset({"MULTI", "EXEC"}),
}


@cache
def _methods(owner: type) -> frozenset[str]:
    return frozenset(name for name in dir(owner) if not name.startswith("_") and callable(getattr(owner, name)))


@cache
def _stdlib() -> frozenset[str]:
    owners = (dict, list, set, str, bytes, Path, os, shutil, json, time, socket.socket, sqlite3.Connection)
    owners += (sqlite3.Cursor, subprocess.Popen, threading.Event, threading.Thread)
    return frozenset().union(*(_methods(owner) for owner in owners))


def _module_name(root: Path, path: Path) -> str:
    parts = path.relative_to(root).with_suffix("").parts
    return ".".join(parts[:-1] if parts[-1] == "__init__" else parts)


def parse(root: Path = ROOT) -> dict:
    return {
        _module_name(root, path): (ast.parse(path.read_text(encoding="utf-8")), path.name == "__init__.py")
        for package in PACKAGES
        for path in sorted((root / package).rglob("*.py"))
    }


def _dotted(node: ast.expr) -> str:
    if isinstance(node, ast.Attribute) and (base := _dotted(node.value)):
        return f"{base}.{node.attr}"
    return node.id if isinstance(node, ast.Name) else ""


def _tail(node: ast.expr) -> str:
    if isinstance(node, ast.Attribute):
        return node.attr
    return node.id if isinstance(node, ast.Name) else ""


def _walk(body: list):
    for statement in body:
        yield from ast.walk(statement)


def _absolute(module: str, is_package: bool, node: ast.ImportFrom) -> str:
    if not node.level:
        base = node.module or ""
        if base.split(".")[0] in PACKAGES:
            return base
        return f"{module if is_package else module.rpartition('.')[0]}.{base}"
    parts = module.split(".") if is_package else module.split(".")[:-1]
    parts = parts[: len(parts) - node.level + 1]
    return ".".join([*parts, *([node.module] if node.module else [])])


def _bind(module: str, is_package: bool, node: ast.AST, scope: dict) -> None:
    if isinstance(node, ast.Import):
        for alias in node.names:
            target = alias.name if alias.asname else alias.name.split(".")[0]
            scope[alias.asname or target] = [f"@{target}"]
    elif isinstance(node, ast.ImportFrom):
        base = _absolute(module, is_package, node)
        for alias in node.names:
            scope[alias.asname or alias.name] = [f"@{base}.{alias.name}"]


def _assigned(node: ast.AST) -> tuple[str, ast.Call] | None:
    if isinstance(node, ast.Assign) and len(node.targets) == 1:
        target, value = node.targets[0], node.value
    elif isinstance(node, ast.withitem):
        target, value = node.optional_vars, node.context_expr
    else:
        return None
    return (target.id, value) if isinstance(target, ast.Name) and isinstance(value, ast.Call) else None


def _main_guard(node: ast.AST) -> bool:
    test = node.test if isinstance(node, ast.If) else None
    return isinstance(test, ast.Compare) and isinstance(test.left, ast.Name) and test.left.id == "__name__"


def _dunder(name: str) -> bool:
    return name.startswith("__") and name.endswith("__")


class Program:
    """Every function, method and module body under the packages, with the names each one reads."""

    def __init__(self, modules: dict):
        self.modules = modules
        self.units: dict[str, Unit] = {}
        self.scopes: dict[str, dict] = {}
        self.members: dict[str, dict[str, str]] = {}
        for module, (tree, is_package) in modules.items():
            self._index(module, tree, is_package)
        self.clients, self.subscribers = self._receivers()

    def _add(self, key: str, module: str, body: list, owner: str = "") -> None:
        unit = self.units[key] = Unit(module, body, owner)
        bound = {
            found[0]: found[1].func.id
            for node in _walk(body)
            if (found := _assigned(node)) and isinstance(found[1].func, ast.Name)
        }
        for node in _walk(body):
            if isinstance(node, ast.Name):
                unit.names.add(node.id)
            elif isinstance(node, ast.Attribute):
                self._attribute(unit, node, bound)

    def _attribute(self, unit: Unit, node: ast.Attribute, bound: dict) -> None:
        if "." in (dotted := _dotted(node)):
            unit.dotted.add(dotted)
        base = node.value.id if isinstance(node.value, ast.Name) else ""
        if unit.owner and base in ("self", "cls"):
            unit.own.add(node.attr)
        elif base in bound:
            unit.typed.add((bound[base], node.attr))
        elif node.attr not in _stdlib():
            unit.methods.add(node.attr)

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
                _bind(module, is_package, node, scope)
        self._add(f"{module}:", module, loose)

    def _class(self, module: str, node: ast.ClassDef) -> str:
        owner = f"{module}:{node.name}"
        methods = [n for n in node.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
        self._add(owner, module, [*node.bases, *(n for n in node.body if n not in methods)])
        members = self.members[owner] = {}
        for item in methods:
            members[item.name] = f"{owner}.{item.name}"
            self._add(members[item.name], module, [item], owner)
        return owner

    def resolve(self, module: str, name: str, scope: dict | None = None) -> list[str]:
        """The units a name read in a module stands for: a function, a class, or what an import binds it to."""
        found = []
        for target in (scope or {}).get(name) or self.scopes.get(module, {}).get(name, []):
            found.extend(self._import(target[1:]) if target.startswith("@") else [target])
        return found

    def _import(self, dotted: str) -> list[str]:
        if dotted in self.modules:
            return [f"{dotted}:"]
        owner, _, name = dotted.rpartition(".")
        if owner in self.modules:
            return [f"{owner}:", *self.resolve(owner, name)]
        return []

    def _nested(self, unit: Unit) -> dict:
        scope: dict = {}
        for node in _walk(unit.body):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                _bind(unit.module, self.modules[unit.module][1], node, scope)
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
                if target.startswith("@"):
                    found.extend(self._import(f"{target[1:]}.{rest.split('.')[0]}"))
        for constructor, name in unit.typed:
            for owner in self.resolve(unit.module, constructor, scope):
                found.extend([self.members[owner][name]] if name in self.members.get(owner, {}) else [])
        members = self.members.get(unit.owner, {})
        return found + [members[name] for name in unit.own if name in members]

    def loose(self, unit: Unit) -> set[str]:
        """Method names a unit reads on objects of no known class, which any reached class may answer."""
        scope = self._nested(unit)
        untyped = {
            name
            for constructor, name in unit.typed
            if name not in _stdlib()
            and not any(owner in self.members for owner in self.resolve(unit.module, constructor, scope))
        }
        return unit.methods | untyped | (unit.own - self.members.get(unit.owner, {}).keys())

    def reach(self, entries: tuple[str, ...]) -> dict[str, str]:
        """Every unit the entries reach, each mapped to the unit that first reached it."""
        return _Reach(self, entries).run()

    def commands(self, keys) -> dict[str, set[str]]:
        found: dict[str, set[str]] = {}
        for key in keys:
            for command in self._issued(self.units[key]):
                found.setdefault(command, set()).add(key)
        return found

    def _issued(self, unit: Unit) -> set[str]:
        sent: set[str] = set()
        for node in _walk(unit.body):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                sent |= lua(node.value)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                sent |= self._call(node, _tail(node.func.value), node.func.attr)
        return sent

    def _call(self, node: ast.Call, receiver: str, name: str) -> frozenset[str]:
        if name == "execute_command" and node.args and isinstance(node.args[0], ast.Constant):
            return frozenset({str(node.args[0].value).upper()})
        if receiver in self.clients and name in HELPERS:
            return HELPERS[name]()
        if receiver in self.clients and name in _methods(redis.Redis):
            return wire(redis.Redis, name)
        if receiver in self.clients and name in _methods(Pipeline):
            return wire(Pipeline, name)
        if receiver in self.subscribers and name in _methods(PubSub):
            return wire(PubSub, name)
        return frozenset()

    def _receivers(self) -> tuple[set[str], set[str]]:
        """Names holding a Redis client, which call a command no function here or in the standard library defines,
        and names bound to a pubsub handle."""
        common = _stdlib() | {name.rpartition(".")[2] for members in self.members.values() for name in members}
        common |= {key.partition(":")[2] for key in self.units}
        clients, subscribers = set(), set()
        for unit in self.units.values():
            for node in _walk(unit.body):
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                    name, receiver = node.func.attr, _tail(node.func.value)
                    if receiver and name not in common and name in _methods(redis.Redis) and wire(redis.Redis, name):
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
            if key not in self.seen and key in self.program.units:
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


def scheduler_keys(deployment: str) -> dict[str, str]:
    from scripts.scheduler.core import Scheduler

    with mock.patch.dict(os.environ, {"AGENTIHOOKS_DEPLOYMENT": deployment}):
        os.environ.pop("AGENTIHOOKS_HIVE_ID", None)
        scheduler = Scheduler(None, machine="probe")
        return {label: scheduler.key(suffix) for label, suffix in SCHEDULER_KEYS.items()}


def consumer_map(deployment: str, program: Program | None = None) -> dict:
    """Each consumer's Redis role, the commands it reaches with the code that sends each, and its scheduler keys."""
    program = program or Program(parse())
    keys = scheduler_keys(deployment)
    consumers = {}
    for name, consumer in CONSUMERS.items():
        found = program.commands(program.reach(consumer.entries))
        commands = {command: sorted(units) for command, units in sorted(found.items())}
        consumers[name] = {"role": consumer.role, "commands": commands, "keys": keys}
    return {"deployment": deployment, "consumers": consumers}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m scripts.swarm.redis_commands", description=__doc__)
    parser.add_argument("--deployment", default="distributed", help="the AGENTIHOOKS_DEPLOYMENT the consumers run in")
    args = parser.parse_args(argv)
    json.dump(consumer_map(args.deployment), sys.stdout, indent=1, sort_keys=True)
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
