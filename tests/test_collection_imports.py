import re
import sys
from functools import cache
from pathlib import Path

import pytest

from tests.shards import discover_test_files

pytestmark = pytest.mark.unit

_ROOT = Path(__file__).parent.parent
HEAVY = "mcp"
_COMMENTS_STRINGS_CONTINUATIONS = re.compile(
    r"#[^\n]*|(\"\"\"|''')(?:\\.|[\s\S])*?\1|\"(?:\\.|[^\"\\\n])*\"|'(?:\\.|[^'\\\n])*'|\\\n"
)
_IMPORT_BODY = (
    r"(?:import\s+(?P<modules>[^\n;]+)|from\s+(?P<module>\w[\w.]*)\s+import\s*(?P<members>\([^)]*\)|[^\n;]+))"
)
_IMPORT = re.compile(r"(?:^|;[ \t]*)" + _IMPORT_BODY, re.MULTILINE)
_ANY_IMPORT = re.compile(r"(?:^[ \t]*|;[ \t]*)" + _IMPORT_BODY, re.MULTILINE)
SDK_GROUP = 'xdist_group("mcp-sdk")'
# build_server imports FastMCP inside the function, out of reach of an import scan.
SDK_CALLS = {"hooks.mcp.build_server"}


@cache
def _module_file(name: str) -> Path | None:
    base = _ROOT.joinpath(*name.split("."))
    for path in (base.with_suffix(".py"), base / "__init__.py"):
        if path.is_file():
            return path
    return None


def _import_names(source: str, pattern: re.Pattern = _IMPORT) -> set[str]:
    names = set()
    for match in pattern.finditer(_COMMENTS_STRINGS_CONTINUATIONS.sub(" ", source)):
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


def loads_sdk_when_run(source: str) -> bool:
    seen: set[str] = set()
    return any(name in SDK_CALLS or loads_heavy(name, seen) for name in _import_names(source, _ANY_IMPORT))


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
# a """ in a comment
QUOTE = '"""'
ESCAPED = "a\\"b"
import sys; import shutil
import glob, \\
    fnmatch
from m1 \\
    import n1
from m2 import(n2)
from m3 import (
    n3,  # n4, n5
)
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
        "sys",
        "shutil",
        "glob",
        "fnmatch",
        "m1",
        "m1.n1",
        "m2",
        "m2.n2",
        "m3",
        "m3.n3",
    }


def test_no_test_module_loads_the_mcp_sdk_while_collecting():
    offenders = [f for f in discover_test_files(_ROOT) if loads_heavy(f.removesuffix(".py").replace("/", "."), set())]
    assert offenders == []


def test_the_run_check_reads_imports_inside_functions():
    assert loads_sdk_when_run("def f():\n    from mcp.types import Tool\n")
    assert loads_sdk_when_run("def f():\n    from hooks.mcp import build_server\n")
    assert not loads_sdk_when_run("def f():\n    from hooks.mcp._session import resolve_session_id\n")


def test_test_modules_that_load_the_mcp_sdk_when_run_share_one_worker():
    sources = {f: (_ROOT / f).read_text() for f in discover_test_files(_ROOT)}
    offenders = [f for f, source in sources.items() if loads_sdk_when_run(source) and SDK_GROUP not in source]
    assert offenders == []
