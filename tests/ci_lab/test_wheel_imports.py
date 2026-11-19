"""Every CI lab module, the CI test and mutation planners and the branch coverage script use shipped packages alone."""

import pkgutil
import subprocess
import sys
from pathlib import Path

import pytest

import scripts.ci_lab

pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[2]
BRANCH_COVERAGE = ["scripts.ci_branch_coverage", "scripts.coverage_check", "scripts.reused_coverage"]
WITHOUT_TESTS = """
import importlib
import sys


class Unshipped:
    def find_spec(self, name: str, path: object = None, target: object = None) -> None:
        if name == "tests" or name.startswith("tests."):
            raise ModuleNotFoundError(f"No module named {name!r}", name=name)
        return None


sys.meta_path.insert(0, Unshipped())
for module in sys.argv[1:]:
    importlib.import_module(module)
"""


def _lab_modules() -> list[str]:
    """Return every module of the CI lab package and the CI test planner."""
    found = pkgutil.walk_packages(scripts.ci_lab.__path__, f"{scripts.ci_lab.__name__}.")
    return sorted(module.name for module in found) + ["scripts.ci_test_plan"]


def _import_without_tests(modules: list[str]) -> subprocess.CompletedProcess[str]:
    """Import the modules in a fresh interpreter whose import system cannot find the tests package."""
    command = [sys.executable, "-c", WITHOUT_TESTS, *modules]
    return subprocess.run(command, cwd=ROOT, capture_output=True, text=True, check=False)


@pytest.mark.feature("lab-wheel-imports")
def test_every_lab_module_imports_without_the_tests_package() -> None:
    modules = _lab_modules()
    assert "scripts.ci_lab.scheduler" in modules
    result = _import_without_tests(modules)
    assert result.returncode == 0, result.stderr


@pytest.mark.feature("branch-coverage-wheel-imports")
def test_the_branch_coverage_script_imports_without_the_tests_package() -> None:
    result = _import_without_tests(BRANCH_COVERAGE)
    assert result.returncode == 0, result.stderr


@pytest.mark.feature("lab-wheel-imports")
def test_an_import_of_the_tests_package_fails_where_the_wheel_is_installed() -> None:
    result = _import_without_tests(["tests.ci_lab"])
    assert result.returncode == 1
    assert "No module named 'tests'" in result.stderr


@pytest.mark.feature("planner-wheel-imports")
def test_the_mutation_planner_checks_test_collection_without_the_tests_package(tmp_path: Path) -> None:
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests/test_sample.py").write_text("import mcp\n")
    check = (
        "from pathlib import Path\n"
        "from scripts.ci_mutation import plan\n"
        f"plan.validate_tests(Path({str(tmp_path)!r}), ['tests/test_sample.py::test_one'])\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", WITHOUT_TESTS + check], cwd=ROOT, capture_output=True, text=True, check=False
    )
    assert result.returncode == 1
    assert result.stderr == "tests/test_sample.py: loads the MCP SDK while collecting\n"
