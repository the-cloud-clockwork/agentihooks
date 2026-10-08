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
    assert "needs" not in sonar
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
    assert "hashFiles(" not in key
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


def _queued_step():
    steps = _workflow()["jobs"]["sonar"]["steps"]
    return next(step for step in steps if step.get("id") == "queued")


@pytest.mark.parametrize(
    ("head_ref", "args"),
    [
        (
            "refs/heads/gh-readonly-queue/dev/pr-1630-f5eb044a8d0c2b1e3f4a5b6c7d8e9f0a1b2c3d4e",
            "-Dsonar.pullrequest.key=1630 -Dsonar.pullrequest.branch=ci-323133-0078 -Dsonar.pullrequest.base=dev",
        ),
        ("refs/heads/dev", None),
    ],
)
def test_queued_merges_are_analysed_as_their_pull_request(tmp_path, head_ref, args):
    steps = _workflow()["jobs"]["sonar"]["steps"]
    step = _queued_step()
    scan = next(s for s in steps if s.get("name") == "SonarQube Scan")
    assert step["if"] == "github.event_name == 'merge_group'"
    assert _workflow()["jobs"]["sonar"]["permissions"]["pull-requests"] == "read"
    assert steps.index(step) < steps.index(scan)
    assert scan["with"]["args"] == "${{ steps.queued.outputs.args }}"
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    gh = bin_dir / "gh"
    gh.write_text('#!/usr/bin/env bash\n[[ "$2" == repos/owner/repo/pulls/1630 ]] && echo ci-323133-0078\n')
    gh.chmod(0o755)
    output = tmp_path / "output"
    output.write_text("")
    env = dict(
        os.environ,
        PATH=f"{bin_dir}:{os.environ['PATH']}",
        GITHUB_REPOSITORY="owner/repo",
        GITHUB_OUTPUT=str(output),
        HEAD_REF=head_ref,
        BASE_REF="refs/heads/dev",
    )
    result = subprocess.run(["bash", "-eo", "pipefail", "-c", step["run"]], env=env, capture_output=True, text=True)
    if args is None:
        assert result.returncode != 0
        assert output.read_text() == ""
    else:
        assert result.returncode == 0, result.stdout + result.stderr
        assert output.read_text() == f"args={args}\n"


_FAKE_GH = """
import json, os, re, sys
from pathlib import Path
state = Path(os.environ["FAKE_STATE"])
spec = json.loads((state / "spec.json").read_text())
tick = int((state / "tick").read_text())
url = sys.argv[2]
made = len((state / "calls").read_text().split())
with (state / "calls").open("a") as calls:
    calls.write(url + "\\n")
if spec.get("fail") or made < spec.get("fail_calls", 0) or (spec.get("fail_jobs") and "/jobs" in url):
    print('{"message": "Server Error"}')
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


def test_wait_gives_up_with_a_warning_when_an_older_run_never_finishes(tmp_path):
    spec = {"runs": [{"id": 5, "run_number": 5, "status": "queued"}], "jobs": {"5": [[]]}}
    result, ticks, _ = _run_wait(tmp_path, spec)
    assert result.returncode == 0, result.stdout + result.stderr
    assert ticks == 36
    assert "::warning::" in result.stdout
    assert " 5;" in result.stdout


def test_wait_is_red_when_the_runs_stay_unreadable(tmp_path):
    result, ticks, _ = _run_wait(tmp_path, {"fail": True, "runs": [], "jobs": {}})
    assert result.returncode != 0
    assert ticks == 36
    assert "::error::" in result.stdout


def test_wait_keeps_polling_through_a_read_outage(tmp_path):
    spec = {
        "fail_calls": 3,
        "runs": [{"id": 5, "run_number": 5, "status": "in_progress"}],
        "jobs": {"5": [_sonar("completed")]},
    }
    result, ticks, _ = _run_wait(tmp_path, spec)
    assert result.returncode == 0, result.stdout + result.stderr
    assert ticks == 3
    assert "unreadable" in result.stdout


def test_wait_does_not_pass_on_an_unreadable_jobs_list(tmp_path):
    spec = {"fail_jobs": True, "runs": [{"id": 5, "run_number": 5, "status": "in_progress"}], "jobs": {"5": [[]]}}
    result, ticks, _ = _run_wait(tmp_path, spec)
    assert result.returncode != 0
    assert ticks == 36


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
