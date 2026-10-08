import json
import os
import subprocess
import sys
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


_FAKE_GH = """
import json, os, re, sys
from pathlib import Path
state = Path(os.environ["FAKE_STATE"])
spec = json.loads((state / "spec.json").read_text())
tick = int((state / "tick").read_text())
url = sys.argv[2]
with (state / "calls").open("a") as calls:
    calls.write(url + "\\n")
if spec.get("fail"):
    sys.exit(1)
if "/workflows/test.yml/runs?" in url:
    print(json.dumps({"workflow_runs": spec["runs"]}))
else:
    states = spec["jobs"][re.search(r"/runs/(\\d+)/jobs", url).group(1)]
    print(json.dumps({"jobs": states[min(tick, len(states) - 1)]}))
"""


def _wait_step():
    steps = _workflow()["jobs"]["sonar"]["steps"]
    return next(step for step in steps if step.get("name") == "Wait for older dev analyses")


def _run_wait(tmp_path, spec, run_number=6):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (tmp_path / "spec.json").write_text(json.dumps(spec))
    (tmp_path / "tick").write_text("0")
    (tmp_path / "calls").write_text("")
    gh = bin_dir / "gh"
    gh.write_text(f"#!{sys.executable}\n{_FAKE_GH}")
    sleep = bin_dir / "sleep"
    sleep.write_text(f'#!/usr/bin/env bash\necho $(( $(cat "{tmp_path}/tick") + 1 )) > "{tmp_path}/tick"\n')
    for tool in (gh, sleep):
        tool.chmod(0o755)
    env = dict(
        os.environ,
        PATH=f"{bin_dir}:{os.environ['PATH']}",
        FAKE_STATE=str(tmp_path),
        GITHUB_REPOSITORY="owner/repo",
        RUN_NUMBER=str(run_number),
    )
    step = _wait_step()
    assert step["shell"] == "bash"
    result = subprocess.run(
        ["timeout", "20", "bash", "--noprofile", "--norc", "-eo", "pipefail", "-c", step["run"]],
        env=env,
        capture_output=True,
        text=True,
    )
    return result, int((tmp_path / "tick").read_text()), (tmp_path / "calls").read_text().split()


def _sonar(status):
    return [{"name": "unit (3.12, 1)", "status": "completed"}, {"name": "sonar", "status": status}]


def test_dev_push_sonar_waits_for_older_runs_before_scanning():
    sonar = _workflow()["jobs"]["sonar"]
    names = [step.get("name") for step in sonar["steps"]]
    assert names.index("Wait for older dev analyses") < names.index("SonarQube Scan")
    step = _wait_step()
    assert step["if"] == "github.event_name == 'push'"
    assert step["env"]["GH_TOKEN"] == "${{ github.token }}"
    assert step["env"]["RUN_NUMBER"] == "${{ github.run_number }}"
    assert "concurrency" not in sonar
    assert sonar["permissions"]["actions"] == "read"


def test_wait_holds_until_every_older_running_sonar_job_completes(tmp_path):
    spec = {
        "runs": [
            {"id": 7, "run_number": 7, "status": "in_progress"},
            {"id": 6, "run_number": 6, "status": "in_progress"},
            {"id": 5, "run_number": 5, "status": "in_progress"},
            {"id": 3, "run_number": 3, "status": "queued"},
            {"id": 2, "run_number": 2, "status": "completed"},
        ],
        "jobs": {
            "5": [_sonar("in_progress"), _sonar("in_progress"), _sonar("completed")],
            "3": [_sonar("completed")],
        },
    }
    result, ticks, calls = _run_wait(tmp_path, spec)
    assert result.returncode == 0, result.stdout + result.stderr
    assert ticks == 2
    assert all("event=push" in url and "branch=dev" in url for url in calls if "/workflows/" in url)
    fetched = {url.split("/runs/")[1].split("/")[0] for url in calls if "/jobs" in url}
    assert fetched == {"5", "3"}


def test_wait_counts_an_older_run_whose_sonar_job_is_not_created_yet(tmp_path):
    spec = {
        "runs": [{"id": 5, "run_number": 5, "status": "in_progress"}],
        "jobs": {"5": [[{"name": "unit (3.12, 1)", "status": "in_progress"}], _sonar("queued"), _sonar("completed")]},
    }
    result, ticks, _ = _run_wait(tmp_path, spec)
    assert result.returncode == 0, result.stdout + result.stderr
    assert ticks == 2


def test_wait_passes_at_once_without_older_running_runs(tmp_path):
    spec = {"runs": [{"id": 9, "run_number": 9, "status": "in_progress"}], "jobs": {}}
    result, ticks, _ = _run_wait(tmp_path, spec)
    assert result.returncode == 0, result.stdout + result.stderr
    assert ticks == 0


def test_wait_is_red_when_the_runs_cannot_be_read(tmp_path):
    result, _, _ = _run_wait(tmp_path, {"fail": True, "runs": [], "jobs": {}})
    assert result.returncode != 0


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
