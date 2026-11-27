"""Live check that the ledger ops name their context by its Protocol: on the installed build, every function of a
module the ledger core dispatches an op to by name takes the op context as ``OpContext``, and the one concrete
context holds every member of the Protocol. The dispatched modules are discovered, never hand kept: every module
held in a module level registry dict of the core, and every module the core's own source hands the context to,
plus the core itself. It prints one line per module and never a credential."""

import ast
import importlib
import inspect
import sys
from collections.abc import Callable, Iterable
from operator import attrgetter, itemgetter
from pathlib import Path
from types import ModuleType
from typing import Any, cast

from scripts.swarm_ledger.op_context import OpContext

LEDGER_PREFIX = "ledger_"
SWARM_LEDGER = "swarm_ledger"

FAIL = "FAIL: "
NAME = "ctx"
TYPED = "{module}: {count} functions take the op context, each by the Protocol"
UNTYPED = "{module}.{function} takes the op context {found}"
NONE = "{module} holds no function that takes the op context"
HELD = "the concrete context holds every member of the Protocol"
LOOSE = "the concrete context lacks a member of the Protocol"
BARE = "untyped"
AS = "as {name}"


def defined(module: ModuleType) -> list[Callable[..., Any]]:
    """The functions a module defines itself, with the methods of the classes it defines."""
    own = [value for value in vars(module).values() if getattr(value, "__module__", None) == module.__name__]
    methods = [value for owner in own if inspect.isclass(owner) for value in vars(owner).values()]
    return [value for value in (*own, *methods) if inspect.isfunction(value)]


def takers(module: ModuleType) -> list[tuple[str, Any]]:
    """Each function of the module that takes the op context, by its qualified name, with the annotation of that
    parameter."""
    found = [(function.__qualname__, inspect.signature(function).parameters.get(NAME)) for function in defined(module)]
    return sorted(((name, taken.annotation) for name, taken in found if taken is not None), key=itemgetter(0))


def written(annotation: Any) -> str:
    """How a failing line names the annotation it found."""
    if annotation is inspect.Parameter.empty:
        return BARE
    return AS.format(name=getattr(annotation, "__name__", repr(annotation)))


def judged(module: ModuleType) -> list[str]:
    """The lines of one module: one failing line per function that takes the op context by anything but the
    Protocol, one failing line when no function takes it, or one line that counts them."""
    name, found = module.__name__.rpartition(".")[2], takers(module)
    if not found:
        return [FAIL + NONE.format(module=name)]
    faults = [
        FAIL + UNTYPED.format(module=name, function=function, found=written(annotation))
        for function, annotation in found
        if annotation is not OpContext
    ]
    return faults or [TYPED.format(module=name, count=len(found))]


def satisfied(context: object) -> str:
    """The line of the concrete context: whether it holds every member of the Protocol."""
    return HELD if isinstance(context, OpContext) else FAIL + LOOSE


def lines(modules: Iterable[ModuleType], context: object) -> list[str]:
    """The lines of every module in name order, then the line of the concrete context."""
    ordered = sorted(modules, key=attrgetter("__name__"))
    return [*(line for module in ordered for line in judged(module)), satisfied(context)]


def importable() -> None:
    """Put the ledger package folder on the import path, once: the ledger core imports its op modules by bare
    name, as the ledger's own entry points arrange before they import it."""
    import scripts.swarm_ledger as package

    if str(package.HERE) not in sys.path:
        sys.path.insert(0, str(package.HERE))


def _bound_ledger_modules(tree: ast.AST) -> dict[str, str]:
    """Every name the tree binds to a ``ledger_*`` module, by `import ledger_x` or
    `from scripts.swarm_ledger import ledger_x`, module level or nested, keyed by the bound name."""
    found: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                last = alias.name.rpartition(".")[2]
                if last.startswith(LEDGER_PREFIX):
                    found[alias.asname or alias.name] = last
        elif isinstance(node, ast.ImportFrom) and node.module and node.module.rpartition(".")[2] == SWARM_LEDGER:
            for alias in node.names:
                if alias.name.startswith(LEDGER_PREFIX):
                    found[alias.asname or alias.name] = alias.name
    return found


def _bound_ledger_functions(tree: ast.AST) -> dict[str, str]:
    """Every name the tree binds to a function imported from a ``ledger_*`` module, by
    `from ledger_x import f` or `from scripts.swarm_ledger.ledger_x import f`, keyed by the bound name to the
    owning module's name."""
    found: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            owner = node.module.rpartition(".")[2]
            if owner.startswith(LEDGER_PREFIX):
                for alias in node.names:
                    found[alias.asname or alias.name] = owner
    return found


def _passes_ctx(call: ast.Call) -> bool:
    """Whether the call hands the name ``ctx`` on, positionally or by keyword."""
    named = (*call.args, *(keyword.value for keyword in call.keywords))
    return any(isinstance(argument, ast.Name) and argument.id == NAME for argument in named)


def handed(tree: ast.AST) -> set[str]:
    """The ``ledger_*`` modules a call in the tree hands the op context to: through an attribute of a module bound
    by import, or by calling a function imported from one."""
    modules, functions = _bound_ledger_modules(tree), _bound_ledger_functions(tree)
    found: set[str] = set()
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and _passes_ctx(node)):
            continue
        func = node.func
        if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name) and func.value.id in modules:
            found.add(modules[func.value.id])
        elif isinstance(func, ast.Name) and func.id in functions:
            found.add(functions[func.id])
    return found


def _registry_modules(core: ModuleType) -> set[ModuleType]:
    """Every module held as a value of a module level dict of the core that registers an op by name."""
    found: set[ModuleType] = set()
    for container in vars(core).values():
        if not isinstance(container, dict) or not container:
            continue
        values: list[Any] = list(cast("dict[Any, Any]", container).values())
        if all(isinstance(value, ModuleType) for value in values):
            found.update(value for value in values if isinstance(value, ModuleType))
    return found


def op_modules(core: ModuleType | None = None) -> list[ModuleType]:
    """Every module the ledger core dispatches an op to by name, discovered with no hand kept list: the core
    itself, every module a registry dict of the core holds, and every ``ledger_*`` module the core's own source
    hands the op context to."""
    if core is None:
        importable()
        from scripts.swarm_ledger import ledger_core

        core = ledger_core
    tree = ast.parse(Path(str(core.__file__)).read_text())
    core_name = core.__name__.rpartition(".")[2]
    handed_names = handed(tree) - {core_name}
    modules = {*_registry_modules(core), *(importlib.import_module(name) for name in sorted(handed_names))}
    return [core, *sorted(modules, key=attrgetter("__name__"))]


def main(modules: Iterable[ModuleType] | None = None, context: object = None) -> int:
    if modules is None:
        importable()
        from scripts.swarm_ledger import ledger_core

        stamps: dict[str, dict[str, Any]] = {}
        modules = op_modules(ledger_core)
        pinned: OpContext = ledger_core.Context({"rev": 0, "stamps": stamps}, 0)
        context = pinned
    found = lines(modules, context)
    for line in found:
        print(line)
    return int(any(line.startswith(FAIL) for line in found))


if __name__ == "__main__":
    sys.exit(main())
