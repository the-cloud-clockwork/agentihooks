"""Runs the mutation selection driver from a protected checkout under the separate package name mutation_grader.

The runner starts this file by path with python -P, so it imports nothing from the pull request tree. Every module of
the checkout's scripts package loads as mutation_grader, with its absolute scripts imports renamed and never from
cached bytecode, so the mutated pull request tree that the tests import as scripts cannot replace any driver code.
"""

import ast
import importlib.abc
import importlib.machinery
import importlib.util
import os
import sys
from pathlib import Path
from types import ModuleType

PACKAGE = "mutation_grader"
DRIVER = f"{PACKAGE}.ci_mutation.selection"


def renamed(tree: ast.Module) -> ast.Module:
    """Return the tree with every absolute scripts import naming the protected package instead."""
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            node.module = protected_name(node.module)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name != protected_name(alias.name) and not alias.asname:
                    raise ImportError(f"import {alias.name} needs an alias to load under {PACKAGE}")
                alias.name = protected_name(alias.name)
    return tree


def protected_name(name: str) -> str:
    """Return the protected package name for a scripts module name, and any other name unchanged."""
    if name == "scripts" or name.startswith("scripts."):
        return PACKAGE + name.removeprefix("scripts")
    return name


class ProtectedLoader(importlib.machinery.SourceFileLoader):
    """Compiles a protected module from its source with its scripts imports renamed."""

    def exec_module(self, module: ModuleType) -> None:
        """Run the module from its source with its scripts imports renamed, never from the checkout's cached bytecode."""
        exec(compile(renamed(ast.parse(self.get_data(self.path))), self.path, "exec"), module.__dict__)


class ProtectedFinder(importlib.abc.MetaPathFinder):
    """Finds the protected package's modules in one checkout's scripts folder."""

    def __init__(self, root: Path) -> None:
        self.root = root

    def find_spec(
        self, fullname: str, path: object = None, target: object = None
    ) -> importlib.machinery.ModuleSpec | None:
        """Return the spec of a protected module from the checkout, or None for any other name."""
        if fullname != PACKAGE and not fullname.startswith(f"{PACKAGE}."):
            return None
        location = self.root.joinpath("scripts", *fullname.split(".")[1:])
        package = (location / "__init__.py").is_file()
        source = location / "__init__.py" if package else location.with_suffix(".py")
        if not source.is_file():
            return None
        return importlib.util.spec_from_file_location(fullname, loader=ProtectedLoader(fullname, str(source)))


def install(root: Path) -> ProtectedFinder:
    """Put a finder for the checkout at root first on the import path and return it."""
    finder = ProtectedFinder(root)
    sys.meta_path.insert(0, finder)
    return finder


def main(root: Path = Path(__file__).resolve().parents[2]) -> None:
    """Run the driver of the checkout at root as the main module, keeping the grader setting from the tests it runs."""
    os.environ.pop("MUTATION_GRADER", None)
    install(root)
    source = str(root / "scripts/ci_mutation/selection.py")
    loader = ProtectedLoader(DRIVER, source)
    driver = importlib.util.module_from_spec(importlib.machinery.ModuleSpec(DRIVER, loader, origin=source))
    driver.__name__ = "__main__"
    sys.modules["__main__"] = sys.modules[DRIVER] = driver
    loader.exec_module(driver)


if __name__ == "__main__":
    main()
