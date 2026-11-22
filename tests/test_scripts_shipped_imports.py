"""No module under scripts imports the tests package, at module level or inside a function or class body."""

import ast
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parent.parent


def _imports_of_tests(source: str) -> list[str]:
    """Return each tests package module the source imports, wherever its import statement sits, in sorted order."""
    if "tests" not in source:
        return []
    found = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            found += [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            found.append(node.module)
    return sorted(name for name in found if name == "tests" or name.startswith("tests."))


@pytest.mark.feature("scripts-import-shipped-packages")
def test_no_scripts_module_imports_the_tests_package() -> None:
    paths = sorted((ROOT / "scripts").rglob("*.py"))
    assert ROOT / "scripts/ci_mutation/plan.py" in paths
    offenders = {str(path.relative_to(ROOT)): names for path in paths if (names := _imports_of_tests(path.read_text()))}
    assert offenders == {}


@pytest.mark.feature("scripts-import-shipped-packages")
def test_the_scan_finds_a_tests_import_in_every_place_and_form() -> None:
    source = """import os, tests
import tests.shards as shards
from tests import dep_manifest
from tests.queue_shards import (
    split,
)
from \\
    tests.flake_ledger import record


def lazy() -> None:
    from tests.test_collection_imports import loads_heavy


class Holder:
    import tests.interaction

    def method(self) -> None:
        import tests.pricing as pricing


try:
    from tests.pass_record import read
except ImportError:
    pass
from . import tests as sibling
from .tests import helper
import testsuite
import tests_helpers
from testsuite import tests
SCRIPT = "import tests"
"""
    assert _imports_of_tests(source) == [
        "tests",
        "tests",
        "tests.flake_ledger",
        "tests.interaction",
        "tests.pass_record",
        "tests.pricing",
        "tests.queue_shards",
        "tests.shards",
        "tests.test_collection_imports",
    ]
    assert _imports_of_tests("import os\nfrom scripts import plan\n") == []
