"""Every tests plugin pytest is told to load with -p imports no hooks or scripts module when it loads.

pytest imports -p plugins before pytest-cov starts measuring in any process, so a graded module such a plugin imports
runs its import time lines unmeasured and the coverage ratchet reads them as lines no test runs.
"""

import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

from scripts.ci_lab.expand_tests import PYTEST_INVOCATION
from tests.coverage_grade import GRADED

pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[1]
RECORD_IMPORTS = """
import importlib
import json
import sys

loaded = set(sys.modules)
importlib.import_module(sys.argv[1])
print(json.dumps(sorted(set(sys.modules) - loaded)))
"""


def _preparse_plugins() -> list[str]:
    """Return every tests plugin the workflows, actions, pytest config and CI lab pass with -p, first named first."""
    configs = [*sorted((ROOT / ".github").rglob("*.y*ml")), ROOT / "pyproject.toml"]
    named = re.findall(r"-p\s*[\"']?(tests\.\w+)", "\n".join(path.read_text() for path in configs))
    lab = [plugin for flag, plugin in zip(PYTEST_INVOCATION, PYTEST_INVOCATION[1:]) if flag == "-p"]
    return list(dict.fromkeys(plugin for plugin in [*named, *lab] if plugin.startswith("tests.")))


def _graded_imports(module: str) -> list[str]:
    """Import the module in a fresh interpreter and return the hooks and scripts modules its import added."""
    command = [sys.executable, "-c", RECORD_IMPORTS, module]
    result = subprocess.run(command, cwd=ROOT, capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr
    packages = {root.rstrip("/") for root in GRADED}
    added: list[str] = json.loads(result.stdout)
    return [name for name in added if name.partition(".")[0] in packages]


@pytest.mark.feature("preparse-plugins-import-no-graded-module")
def test_no_preparse_plugin_imports_a_hooks_or_scripts_module_when_it_loads() -> None:
    plugins = _preparse_plugins()
    assert "tests.flake_ledger" in plugins
    assert {plugin: _graded_imports(plugin) for plugin in plugins} == {plugin: [] for plugin in plugins}


@pytest.mark.feature("preparse-plugins-import-no-graded-module")
def test_the_import_check_reports_each_graded_module_an_import_adds() -> None:
    assert _graded_imports("json") == []
    assert {"scripts", "scripts.gh_config"} <= set(_graded_imports("scripts.gh_config"))
