import re
import sys
from functools import cache
from pathlib import Path

import pytest

from tests.shards import discover_test_files

pytestmark = pytest.mark.unit

_ROOT = Path(__file__).parent.parent
HEAVY = "mcp"
_TRIPLE_QUOTED = re.compile(r"(\"\"\"|''')[\s\S]*?\1")
_IMPORT = re.compile(
    r"^(?:import\s+(?P<modules>[^\n#;]+)|from\s+(?P<module>\w[\w.]*)\s+import\s+(?P<members>\([^)]*\)|[^\n#;]+))",
    re.MULTILINE,
)


@cache
def _module_file(name: str) -> Path | None:
    base = _ROOT.joinpath(*name.split("."))
    for path in (base.with_suffix(".py"), base / "__init__.py"):
        if path.is_file():
            return path
    return None


def _import_names(source: str) -> set[str]:
    names = set()
    for match in _IMPORT.finditer(_TRIPLE_QUOTED.sub("", source)):
        if match["modules"]:
            names |= {alias.split()[0] for alias in match["modules"].split(",")}
        else:
            module = match["module"]
            names.add(module)
            members = match["members"].strip("() \t").replace("\n", " ").split(",")
            names |= {f"{module}.{alias.split()[0]}" for alias in members if alias.strip()}
    return names


@cache
def _module_level_imports(path: Path) -> frozenset[str]:
    names = _import_names(path.read_text())
    return frozenset(".".join(parts[:i]) for parts in (n.split(".") for n in names) for i in range(1, len(parts) + 1))


def loads_heavy(name: str, seen: set[str]) -> bool:
    if name in seen:
        return False
    seen.add(name)
    if name.split(".")[0] == HEAVY:
        return True
    path = _module_file(name)
    parents = {name.rsplit(".", i)[0] for i in range(1, name.count(".") + 1)}
    return path is not None and any(loads_heavy(dep, seen) for dep in parents | _module_level_imports(path))


def test_the_check_follows_a_package_init_to_a_heavy_module(monkeypatch):
    monkeypatch.setattr(sys.modules[__name__], "HEAVY", "importlib")
    assert loads_heavy("hooks.mcp._session", set())
    assert not loads_heavy("hooks.common", set())


def test_the_mcp_tables_load_without_the_sdk():
    assert not loads_heavy("hooks.mcp._session", set())
    assert not loads_heavy("hooks.mcp._registry", set())


def test_the_import_scan_reads_only_module_level_absolute_imports():
    source = '''from __future__ import annotations
import os, json as j
import a.b as c  # note
from x.y import (
    p,
    q as r,
)
from . import sibling
from .rel import thing
SCRIPT = """
import mcp
from mcp import server
"""
try:
    import mcp
except ImportError:
    pass


def f():
    from mcp import types
'''
    assert _import_names(source) == {
        "__future__",
        "__future__.annotations",
        "os",
        "json",
        "a.b",
        "x.y",
        "x.y.p",
        "x.y.q",
    }


def test_no_test_module_loads_the_mcp_sdk_while_collecting():
    offenders = [f for f in discover_test_files(_ROOT) if loads_heavy(f.removesuffix(".py").replace("/", "."), set())]
    assert offenders == []
