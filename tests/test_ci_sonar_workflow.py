import json
import os
import subprocess
from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.unit
_ROOT = Path(__file__).resolve().parents[1]


def _workflow():
    return yaml.safe_load((_ROOT / ".github/workflows/test.yml").read_text())


def test_sonar_is_required_on_dev_and_main_pull_requests():
    workflow = _workflow()
    event = workflow[True]["pull_request"]
    assert set(event["branches"]) == {"dev", "main"}
    assert "paths" not in event
    assert "paths-ignore" not in event
    jobs = workflow["jobs"]
    assert set(jobs["gate-required"]["needs"]) == {"unit", "lint", "sonar"}
    sonar = jobs["sonar"]
    assert sonar["needs"] == ["unit"]
    assert "if" not in sonar
    assert not sonar.get("continue-on-error")
    assert sonar["uses"] == "The-Cloud-Clockwork/.github/.github/workflows/sonar-reusable.yml@main"
    assert "needs" not in jobs["lint"]
    assert "needs" not in jobs["unit"]


@pytest.mark.parametrize("sonar", ["success", "failure", "skipped", "cancelled", "pending"])
def test_required_gate_rejects_unsuccessful_sonar(sonar):
    step = _workflow()["jobs"]["gate-required"]["steps"][0]
    needs = {"unit": {"result": "success"}, "lint": {"result": "success"}, "sonar": {"result": sonar}}
    result = subprocess.run(
        ["bash", "-e", "-c", step["run"]],
        env=dict(os.environ, NEEDS=json.dumps(needs)),
        capture_output=True,
        text=True,
    )
    assert (result.returncode == 0) == (sonar == "success"), result.stdout + result.stderr
    if result.returncode:
        assert "::error::" in result.stdout


def test_secret_detection_includes_all_tracked_text_and_hidden_configuration():
    properties = dict(
        line.split("=", 1)
        for line in (_ROOT / "sonar-project.properties").read_text().splitlines()
        if line and not line.startswith("#")
    )
    assert properties["sonar.text.activate"] == "true"
    assert properties["sonar.text.inclusions.activate"] == "true"
    assert properties["sonar.text.inclusions"] == "**/*"
    assert properties["sonar.scanner.excludeHiddenFiles"] == "false"
