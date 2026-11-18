"""Every tests plugin a workflow loads with -p imports no hooks or scripts module when it loads.

pytest imports -p plugins before pytest-cov starts measuring in any process, so a graded module such a plugin imports
runs its import time lines unmeasured and the coverage ratchet reads them as lines no test runs.
"""

import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

from tests.coverage_grade import GRADED

pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[1]
IMPORTED = """
import importlib
import json
import sys

imported = {}
for module in sys.argv[1:]:
    loaded = set(sys.modules)
    importlib.import_module(module)
    imported[module] = sorted(set(sys.modules) - loaded)
print(json.dumps(imported))
"""


def _workflow_plugins() -> list[str]:
    """Return every tests plugin the workflows load with -p, in the order they first name it."""
    text = "\n".join(path.read_text() for path in sorted((ROOT / ".github" / "workflows").glob("*.yml")))
    return list(dict.fromkeys(re.findall(r"-p (tests\.\w+)", text)))


def _graded_imports(modules: list[str]) -> dict[str, list[str]]:
    """Import the modules in order in a fresh interpreter and return the hooks and scripts modules each one added."""
    command = [sys.executable, "-c", IMPORTED, *modules]
    result = subprocess.run(command, cwd=ROOT, capture_output=True, text=True, check=False)
    assert result.returncode == 0, result.stderr
    packages = {root.rstrip("/") for root in GRADED}
    imported: dict[str, list[str]] = json.loads(result.stdout)
    return {
        module: [name for name in names if name.partition(".")[0] in packages] for module, names in imported.items()
    }


@pytest.mark.feature("workflow-plugins-import-no-graded-module")
def test_no_workflow_plugin_imports_a_hooks_or_scripts_module_when_it_loads() -> None:
    plugins = _workflow_plugins()
    assert "tests.flake_ledger" in plugins
    assert _graded_imports(plugins) == {plugin: [] for plugin in plugins}


@pytest.mark.feature("workflow-plugins-import-no-graded-module")
def test_the_import_check_reports_each_graded_module_an_import_adds() -> None:
    imported = _graded_imports(["json", "scripts.gh_config"])
    assert imported["json"] == []
    assert {"scripts", "scripts.gh_config"} <= set(imported["scripts.gh_config"])
