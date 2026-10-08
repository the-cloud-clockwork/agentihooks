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
    assert "sonar" in jobs["gate-required"]["needs"]
    sonar = jobs["sonar"]
    assert sonar["needs"] == ["unit"]
    assert "if" not in sonar
    assert not sonar.get("continue-on-error")
    gate = next(step for step in sonar["steps"] if step.get("uses") == "sonarsource/sonarqube-quality-gate-action@v1")
    assert "if" not in gate
    assert not gate.get("continue-on-error")
    assert "needs" not in jobs["lint"]
    assert "needs" not in jobs["unit"]


def test_sonar_restores_downloads_before_every_scan():
    steps = _workflow()["jobs"]["sonar"]["steps"]
    scan_index = next(i for i, step in enumerate(steps) if step.get("name") == "SonarQube Scan")
    cache = next(step for step in steps[:scan_index] if step.get("uses") == "actions/cache@v4")
    assert set(cache["with"]["path"].splitlines()) == {
        "~/.sonar/cache",
        "${{ runner.tool_cache }}/sonar-scanner-cli",
    }
    key = cache["with"]["key"]
    assert "${{ runner.os }}" in key
    assert "${{ runner.arch }}" in key
    assert "hashFiles('.github/workflows/test.yml')" in key
    assert "if" not in cache
    assert "if" not in steps[scan_index]
    assert not cache.get("continue-on-error")


def test_sonar_download_cache_tracks_scanner_and_server_versions():
    sonar = _workflow()["jobs"]["sonar"]
    steps = sonar["steps"]
    cache = next(step for step in steps if step.get("name") == "Restore Sonar downloads")
    scan = next(step for step in steps if step.get("name") == "SonarQube Scan")
    proxy = next(step for step in steps if step.get("id") == "proxy")
    assert sonar["env"]["SONAR_SCANNER_VERSION"]
    assert scan["with"]["scannerVersion"] == "${{ env.SONAR_SCANNER_VERSION }}"
    assert "${{ env.SONAR_SCANNER_VERSION }}" in cache["with"]["key"]
    assert "${{ steps.proxy.outputs.version }}" in cache["with"]["key"]
    assert "/api/server/version" in proxy["run"]
    assert '>> "$GITHUB_OUTPUT"' in proxy["run"]


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
    assert properties["sonar.sources"] == "."
    assert "tests/**" in properties["sonar.exclusions"].split(",")
