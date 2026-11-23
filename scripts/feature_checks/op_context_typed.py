"""Live check that the ledger ops name their context by its Protocol: on the installed build, every function of a
module registered in ``ledger_core.EXTENSION_OPS`` that takes the op context annotates it ``OpContext``, and the one
concrete context holds every member of the Protocol. It prints one line per module and never a credential."""

import inspect
import sys
from collections.abc import Callable, Iterable
from operator import attrgetter, itemgetter
from types import ModuleType
from typing import Any

from scripts.swarm_ledger.op_context import OpContext

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


def main(modules: Iterable[ModuleType] | None = None, context: object = None) -> int:
    if modules is None:
        importable()
        from scripts.swarm_ledger import ledger_core

        stamps: dict[str, dict[str, Any]] = {}
        modules = set(ledger_core.EXTENSION_OPS.values())
        context = ledger_core.Context({"rev": 0, "stamps": stamps}, 0)
    found = lines(modules, context)
    for line in found:
        print(line)
    return int(any(line.startswith(FAIL) for line in found))


if __name__ == "__main__":
    sys.exit(main())
