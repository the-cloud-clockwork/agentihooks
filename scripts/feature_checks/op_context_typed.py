"""Live check that the ledger ops name their context by its Protocol: on the installed build, every function of a
module the ledger core dispatches an op to by name takes the op context as ``OpContext``, and the one concrete
context holds every member of the Protocol. The dispatched modules are discovered, never hand kept: every module
held in a module level registry dict of the core, and every module the core's own source hands the context to,
plus the core itself. It prints one line per module and never a credential."""

import ast
import importlib
import importlib.machinery
import inspect
import sys
from collections.abc import Callable, Iterable
from operator import attrgetter, itemgetter
from pathlib import Path
from types import ModuleType
from typing import Any, cast

from scripts.swarm_ledger.op_context import OpContext

FAIL = "FAIL: "
NAME = "ctx"
TYPED = "{module}: {count} functions take the op context, each by the Protocol"
UNTYPED = "{module}.{function} takes the op context {found}"
NONE = "{module} holds no function that takes the op context"
REBOUND = "{module}.{function} renames the op context to {name}"
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


def _bound_modules(tree: ast.AST) -> dict[str, str]:
    """Every name the tree binds to a module, by `import x [as n]` or `from pkg import mod [as n]`, module level
    or inside a function, keyed by the bound name to the dotted module path to resolve."""
    found: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                found[alias.asname or alias.name.partition(".")[0]] = alias.name
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            for alias in node.names:
                found[alias.asname or alias.name] = f"{node.module}.{alias.name}"
    return found


def _bound_functions(tree: ast.AST) -> dict[str, str]:
    """Every name the tree binds to something imported from a module and called bare, by `from pkg import f [as
    n]`, keyed by the bound name to the owning module's dotted path."""
    found: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            for alias in node.names:
                found[alias.asname or alias.name] = node.module
    return found


def _passes_ctx(call: ast.Call) -> bool:
    """Whether the call hands the name ``ctx`` on, positionally or by keyword."""
    named = (*call.args, *(keyword.value for keyword in call.keywords))
    return any(isinstance(argument, ast.Name) and argument.id == NAME for argument in named)


def _spec_origin(dotted: str) -> str | None:
    """The source file the import path holds for the dotted path, without importing it, or None when it holds
    none. The path finder is asked alone, in the folders of the parent package for a dotted name."""
    parent = dotted.rpartition(".")[0]
    try:
        search = getattr(importlib.import_module(parent), "__path__", ()) if parent else None
    except ImportError:
        return None
    found = importlib.machinery.PathFinder.find_spec(dotted, search)
    return found.origin if found is not None else None


def _inside(origin: str | None, folder: Path) -> bool:
    """Whether the origin file lies inside the given folder, or a folder below it."""
    return origin is not None and folder in Path(origin).resolve().parents


def handed(tree: ast.AST, folder: Path) -> set[ModuleType]:
    """Every module whose source file lives inside the given folder, or a folder below it, that a call in the
    tree hands the op context to: through an attribute of a module bound by import, or by calling something
    bound bare from a `from` import. Only a module already placed inside the folder is imported; an import that
    then raises is left to raise, the same as it would for the core that dispatches to it."""
    modules, functions = _bound_modules(tree), _bound_functions(tree)
    found: set[ModuleType] = set()
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and _passes_ctx(node)):
            continue
        func = node.func
        dotted = None
        if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name) and func.value.id in modules:
            dotted = modules[func.value.id]
        elif isinstance(func, ast.Name) and func.id in functions:
            dotted = functions[func.id]
        if dotted is None:
            continue
        if _inside(_spec_origin(dotted), folder):
            found.add(importlib.import_module(dotted))
    return found


def _renamed_ctx(node: ast.AST) -> list[str]:
    """The names a statement or expression binds the op context to, under its own name."""
    if isinstance(node, ast.Assign) and isinstance(node.value, ast.Name) and node.value.id == NAME:
        return [target.id for target in node.targets if isinstance(target, ast.Name)]
    if (
        isinstance(node, (ast.AnnAssign, ast.NamedExpr))
        and isinstance(node.target, ast.Name)
        and isinstance(node.value, ast.Name)
        and node.value.id == NAME
    ):
        return [node.target.id]
    return []


def rebound(tree: ast.AST, module: str) -> list[str]:
    """One failing line per function of the module that binds the op context to another name."""
    found: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for inner in ast.walk(node):
            found.extend(
                FAIL + REBOUND.format(module=module, function=node.name, name=name) for name in _renamed_ctx(inner)
            )
    return found


def _registry_modules(core: ModuleType) -> set[ModuleType]:
    """Every module held as a value of a module level dict of the core that registers an op by name."""
    found: set[ModuleType] = set()
    for container in vars(core).values():
        if not isinstance(container, dict):
            continue
        values: list[Any] = list(cast("dict[Any, Any]", container).values())
        found.update(value for value in values if isinstance(value, ModuleType))
    return found


def op_modules(core: ModuleType | None = None) -> list[ModuleType]:
    """Every module the ledger core dispatches an op to by name, discovered with no hand kept list: the core
    itself, every module a registry dict of the core holds, and every module inside the ledger package folder
    that the core's own source hands the op context to."""
    if core is None:
        importable()
        from scripts.swarm_ledger import ledger_core

        core = ledger_core
    path = Path(str(core.__file__)).resolve()
    tree = ast.parse(path.read_text())
    modules = {*_registry_modules(core), *handed(tree, path.parent)}
    return [core, *sorted(modules, key=attrgetter("__name__"))]


def main(modules: Iterable[ModuleType] | None = None, context: object = None) -> int:
    extra: list[str] = []
    if modules is None:
        importable()
        from scripts.swarm_ledger import ledger_core

        stamps: dict[str, dict[str, Any]] = {}
        modules = op_modules()
        pinned: OpContext = ledger_core.Context({"rev": 0, "stamps": stamps}, 0)
        context = pinned
        core_path = Path(str(ledger_core.__file__)).resolve()
        extra = rebound(ast.parse(core_path.read_text()), ledger_core.__name__.rpartition(".")[2])
    computed = lines(modules, context)
    found = [*computed[:-1], *extra, computed[-1]]
    for line in found:
        print(line)
    return int(any(line.startswith(FAIL) for line in found))


if __name__ == "__main__":
    sys.exit(main())
