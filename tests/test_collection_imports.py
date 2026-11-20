import time
from pathlib import Path

import pytest

from scripts import collection_imports
from scripts.collection_imports import (
    SDK_GROUP,
    _function_imports,
    _import_names,
    calls_heavy,
    loads_heavy,
    loads_sdk_when_run,
)
from tests.shards import discover_test_files

pytestmark = pytest.mark.unit

_ROOT = Path(__file__).parent.parent


def test_the_check_follows_a_package_init_to_a_heavy_module(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(collection_imports, "HEAVY", "importlib")
    assert loads_heavy(_ROOT, "hooks.mcp._session", set())
    assert not loads_heavy(_ROOT, "hooks.common", set())


def test_the_mcp_tables_load_without_the_sdk() -> None:
    assert not loads_heavy(_ROOT, "hooks.mcp._session", set())
    assert not loads_heavy(_ROOT, "hooks.mcp._registry", set())


def test_the_check_follows_a_dotted_import_to_its_submodule(tmp_path: Path) -> None:
    (tmp_path / "plain").mkdir()
    (tmp_path / "plain/__init__.py").write_text("")
    (tmp_path / "plain/heavy.py").write_text("import mcp\n")
    (tmp_path / "direct.py").write_text("import plain.heavy\n")
    assert loads_heavy(tmp_path, "direct", set())


def test_the_check_loads_the_package_of_a_missing_submodule(tmp_path: Path) -> None:
    (tmp_path / "eager").mkdir()
    (tmp_path / "eager/__init__.py").write_text("import mcp\n")
    (tmp_path / "missing.py").write_text("from eager.absent import thing\n")
    assert loads_heavy(tmp_path, "missing", set())


def test_an_import_never_reads_the_package_at_the_root(tmp_path: Path) -> None:
    (tmp_path / "__init__.py").write_text("import mcp\n")
    (tmp_path / "light.py").write_text("import os\n")
    assert not loads_heavy(tmp_path, "light", set())


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


def test_the_import_scan_keeps_every_letter_of_a_member_name() -> None:
    assert _import_names("from m import X\n") == {"m", "m.X"}


def test_the_import_scan_keeps_an_escaped_triple_quote_inside_its_string() -> None:
    assert _import_names('x = """a\n\\"""\nimport mcp\n"""\nimport os\n') == {"os"}


@pytest.mark.wall_clock
def test_the_import_scan_reads_an_unterminated_string_of_escapes_in_linear_time() -> None:
    started = time.perf_counter()
    _import_names('"""' + "\\x" * 26)
    assert time.perf_counter() - started < 0.5


def test_no_test_module_loads_the_mcp_sdk_while_collecting() -> None:
    clean: set[str] = set()
    offenders: list[str] = []
    for f in discover_test_files(_ROOT):
        walked = set(clean)
        if loads_heavy(_ROOT, f.removesuffix(".py").replace("/", "."), walked):
            offenders.append(f)
        else:
            clean = walked
    assert offenders == []


def test_the_run_check_reads_imports_inside_functions() -> None:
    assert loads_sdk_when_run(_ROOT, "def f():\n    from mcp.types import Tool\n")
    assert loads_sdk_when_run(_ROOT, "def f():\n    from hooks.mcp import build_server\n")
    assert not loads_sdk_when_run(_ROOT, "def f():\n    from hooks.mcp._session import resolve_session_id\n")
    assert not loads_sdk_when_run(_ROOT, "def f():\n    from hooks.mcp import resolve_transport\n")


def test_a_bare_name_calls_no_function_of_the_root_package(tmp_path: Path) -> None:
    (tmp_path / "__init__.py").write_text("def run():\n    import mcp\n")
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg/__init__.py").write_text("def run():\n    import mcp\n")
    assert calls_heavy(tmp_path, "pkg.run", set())
    assert not calls_heavy(tmp_path, "run", set())
    assert not loads_sdk_when_run(tmp_path, "import run\n")


def test_function_imports_read_each_module_once(tmp_path, monkeypatch):
    path = tmp_path / "functions.py"
    path.write_text("def first():\n    import mcp\n\nasync def second():\n    import os\n")
    reads = []
    read_text = Path.read_text

    def read(module):
        reads.append(module)
        return read_text(module)

    monkeypatch.setattr(Path, "read_text", read)
    assert _function_imports(path, "first") == {"mcp"}
    assert _function_imports(path, "second") == {"os"}
    assert _function_imports(path, "missing") == set()
    assert reads == [path]


def test_function_imports_read_past_a_signature_split_over_lines(tmp_path: Path) -> None:
    path = tmp_path / "functions.py"
    path.write_text("def run(\n    value: int,\n) -> None:\n    import mcp\n")
    assert _function_imports(path, "run") == {"mcp"}


def test_test_modules_that_load_the_mcp_sdk_when_run_share_one_worker() -> None:
    clean: set[str] = set()
    offenders: list[str] = []
    for f in discover_test_files(_ROOT):
        source = (_ROOT / f).read_text()
        walked = set(clean)
        if not loads_sdk_when_run(_ROOT, source, walked):
            clean = walked
        elif SDK_GROUP not in source:
            offenders.append(f)
    assert offenders == []
