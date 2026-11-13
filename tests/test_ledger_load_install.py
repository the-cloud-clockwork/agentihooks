from pathlib import Path

import pytest
import yaml

WORKFLOW = Path(__file__).resolve().parents[1] / ".github/workflows/ledger-load.yml"
pytestmark = pytest.mark.unit


def test_every_ledger_load_install_uses_a_trusted_version_without_scanning_git() -> None:
    jobs = yaml.safe_load(WORKFLOW.read_text())["jobs"]
    for name in ("load", "compare"):
        [install] = [step for step in jobs[name]["steps"] if step.get("name") == "Install dependencies"]
        assert install["env"]["SETUPTOOLS_SCM_PRETEND_VERSION_FOR_AGENTIHOOKS"] == "0.dev${{ github.run_number }}"
        assert '-e ".[all]"' in install["run"]
