import ast
import sys
from functools import cache
from pathlib import Path

import pytest

from tests.shards import discover_test_files

pytestmark = pytest.mark.unit

_ROOT = Path(__file__).parent.parent
HEAVY = "mcp"


def _module_file(name: str) -> Path | None:
    base = _ROOT.joinpath(*name.split("."))
    for path in (base.with_suffix(".py"), base / "__init__.py"):
        if path.is_file():
            return path
    return None


@cache
def _module_level_imports(path: Path) -> frozenset[str]:
    names = set()
    for node in ast.parse(path.read_text()).body:
        if isinstance(node, ast.Import):
            names |= {alias.name for alias in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            names.add(node.module)
            names |= {f"{node.module}.{a.name}" for a in node.names if _module_file(f"{node.module}.{a.name}")}
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


def test_the_check_follows_a_package_init_to_the_sdk(monkeypatch):
    monkeypatch.setattr(sys.modules[__name__], "HEAVY", "importlib")
    assert loads_heavy("hooks.mcp._session", set())
    assert not loads_heavy("hooks.common", set())


def test_the_mcp_tables_load_without_the_sdk():
    assert not loads_heavy("hooks.mcp._session", set())
    assert not loads_heavy("hooks.mcp._registry", set())


def test_no_test_module_loads_the_mcp_sdk_while_collecting():
    offenders = [f for f in discover_test_files(_ROOT) if loads_heavy(f.removesuffix(".py").replace("/", "."), set())]
    assert offenders == []
