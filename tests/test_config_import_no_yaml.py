import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.feature("config-import-without-yaml")
def test_hooks_config_imports_without_pyyaml_installed():
    result = subprocess.run(
        [sys.executable, "-c", "import sys; sys.modules['yaml'] = None; import hooks.config"],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
