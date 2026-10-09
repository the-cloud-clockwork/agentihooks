import json
import os
import subprocess
from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize(
    ("binding", "api_status", "expected"),
    [
        ({"qualityGate": {"name": "Delivery L2"}}, 0, 0),
        ({"qualityGate": {"name": "Sonar way"}}, 0, 1),
        ({"qualityGate": {"name": "Delivery L2 copy"}}, 0, 1),
        ({"qualityGate": {}}, 0, 1),
        ({"errors": [{"msg": "Insufficient privileges"}]}, 22, 1),
    ],
)
def test_sonar_requires_the_bound_gate_with_a_separate_reader(tmp_path, binding, api_status, expected):
    workflow = yaml.safe_load((ROOT / ".github/workflows/test.yml").read_text())
    steps = workflow["jobs"]["sonar"]["steps"]
    step = next((s for s in steps if s.get("name") == "Hold the Delivery L2 binding"), None)
    assert step is not None
    assert step["env"] == {"SONAR_READ_TOKEN": "${{ secrets.SONAR_READ_TOKEN }}"}
    assert step.get("continue-on-error", False) is False
    assert step["if"] == "steps.current.outputs.superseded != 'true'"
    assert steps.index(next(s for s in steps if s.get("id") == "proxy")) < steps.index(step)
    assert steps.index(step) < steps.index(next(s for s in steps if s.get("name") == "SonarQube Scan"))
    curl = tmp_path / "curl"
    curl.write_text(
        "#!/usr/bin/env bash\nset -euo pipefail\n"
        '[[ "$*" == *"Authorization: Bearer reader-test"* ]]\n'
        '[[ "${*: -1}" == "http://127.0.0.1:9000/api/qualitygates/get_by_project?project=agentihooks" ]]\n'
        'printf "%s" "$BINDING"\nexit "$API_STATUS"\n'
    )
    curl.chmod(0o755)
    result = subprocess.run(
        ["bash", "-eo", "pipefail", "-c", step["run"]],
        env=dict(
            os.environ,
            PATH=f"{tmp_path}:{os.environ['PATH']}",
            SONAR_READ_TOKEN="reader-test",
            BINDING=json.dumps(binding),
            API_STATUS=str(api_status),
        ),
        capture_output=True,
        text=True,
    )
    assert (result.returncode == 0) == (expected == 0), result.stdout + result.stderr
    if expected == 0:
        assert "Project agentihooks is bound to Delivery L2" in result.stdout
    elif api_status == 0 and binding.get("qualityGate", {}).get("name"):
        assert "::error::" in result.stdout


def test_scan_and_analysis_still_use_the_scan_token():
    workflow = yaml.safe_load((ROOT / ".github/workflows/test.yml").read_text())
    steps = workflow["jobs"]["sonar"]["steps"]
    for name in ("SonarQube Scan", "SonarQube Quality Gate", "Hold the Delivery L2 conditions"):
        step = next(s for s in steps if s.get("name") == name)
        assert step["env"]["SONAR_TOKEN"] == "${{ secrets.SONAR_TOKEN }}"


def test_missing_reader_secret_cannot_pass_as_an_anonymous_public_project_read(tmp_path):
    workflow = yaml.safe_load((ROOT / ".github/workflows/test.yml").read_text())
    step = next(s for s in workflow["jobs"]["sonar"]["steps"] if s.get("name") == "Hold the Delivery L2 binding")
    curl = tmp_path / "curl"
    curl.write_text('#!/usr/bin/env bash\nprintf \'%s\' \'{"qualityGate":{"name":"Delivery L2"}}\'\n')
    curl.chmod(0o755)
    result = subprocess.run(
        ["bash", "-eo", "pipefail", "-c", step["run"]],
        env=dict(os.environ, PATH=f"{tmp_path}:{os.environ['PATH']}", SONAR_READ_TOKEN=""),
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0, result.stdout + result.stderr
