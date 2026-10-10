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
    assert sonar["needs"] == ["unit", "reuse"]
    assert (
        sonar["if"]
        == "${{ !cancelled() && (github.event_name != 'merge_group' || needs.reuse.outputs.reused != 'true') }}"
    )
    assert not sonar.get("continue-on-error")
    gate = next(step for step in sonar["steps"] if step.get("name") == "SonarQube Quality Gate")
    assert gate["if"] == "steps.current.outputs.superseded != 'true'"
    assert not gate.get("continue-on-error")
    assert jobs["lint"]["needs"] == ["reuse"]
    assert jobs["unit"]["needs"] == ["split", "reuse"]


def test_sonar_restores_downloads_before_every_scan():
    steps = _workflow()["jobs"]["sonar"]["steps"]
    scan_index = next(i for i, step in enumerate(steps) if step.get("name") == "SonarQube Scan")
    cache = next(step for step in steps[:scan_index] if step.get("name") == "Restore Sonar downloads")
    assert set(cache["with"]["path"].splitlines()) == {
        "~/.sonar/cache",
        "${{ runner.tool_cache }}/sonar-scanner-cli",
    }
    key = cache["with"]["key"]
    assert "${{ runner.os }}" in key
    assert "${{ runner.arch }}" in key
    assert "hashFiles(" not in key
    assert "if" not in cache
    assert steps[scan_index]["if"] == "steps.current.outputs.superseded != 'true'"
    assert not cache.get("continue-on-error")


def test_sonar_download_cache_tracks_scanner_and_server_versions():
    sonar = _workflow()["jobs"]["sonar"]
    steps = sonar["steps"]
    cache = next(step for step in steps if step.get("name") == "Restore Sonar downloads")
    scan = next(step for step in steps if step.get("name") == "SonarQube Scan")
    proxy = next(step for step in steps if step.get("id") == "proxy")
    assert sonar["env"]["SONAR_SCANNER_VERSION"]
    assert "$SONAR_SCANNER_VERSION" in scan["run"]
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
    ("head_ref", "later", "args"),
    [
        (
            "refs/heads/gh-readonly-queue/dev/pr-1630-f5eb044a8d0c2b1e3f4a5b6c7d8e9f0a1b2c3d4e",
            "ccc",
            "-Dsonar.pullrequest.key=1630 -Dsonar.pullrequest.branch=ci-323133-0078 -Dsonar.pullrequest.base=dev",
        ),
        (
            "refs/heads/gh-readonly-queue/dev/pr-1630-f5eb044a8d0c2b1e3f4a5b6c7d8e9f0a1b2c3d4e",
            "bbb",
            "-Dsonar.pullrequest.key=1630 -Dsonar.pullrequest.branch=pr-1630 -Dsonar.pullrequest.base=dev",
        ),
        ("refs/heads/dev", "ccc", None),
    ],
    ids=["one-branch-holds-the-head", "two-branches-share-the-head", "not-a-queue-ref"],
)
def test_queued_merges_are_analysed_as_their_pull_request(tmp_path, head_ref, later, args):
    steps = _workflow()["jobs"]["sonar"]["steps"]
    step = _queued_step()
    scan = next(s for s in steps if s.get("name") == "SonarQube Scan")
    assert step["if"] == "github.event_name == 'merge_group'"
    assert "pull-requests" not in _workflow()["jobs"]["sonar"]["permissions"]
    assert "GH_TOKEN" not in step["env"]
    assert steps.index(step) < steps.index(scan)
    assert scan["env"]["ARGS"] == "${{ steps.queued.outputs.args || steps.dispatched.outputs.args }}"
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    git = bin_dir / "git"
    git.write_text(
        "#!/usr/bin/env bash\n"
        '[[ "$1" == ls-remote && "$*" == *origin* ]] || exit 1\n'
        'if [[ "$*" == *refs/pull/1630/head* ]]; then printf "bbb\\trefs/pull/1630/head\\n"; exit; fi\n'
        '[[ "$*" == *--heads* ]] || exit 1\n'
        f'printf "aaa\\trefs/heads/dev\\nbbb\\trefs/heads/ci-323133-0078\\n{later}\\trefs/heads/later\\n"\n'
    )
    git.chmod(0o755)
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


@pytest.mark.parametrize(
    ("ref_name", "args"),
    [
        (
            "diffcheck/plant-1",
            "-Dsonar.pullrequest.key=dispatch-diffcheck-plant-1 -Dsonar.pullrequest.branch=diffcheck/plant-1 -Dsonar.pullrequest.base=dev",
        ),
        (
            "feature-x",
            "-Dsonar.pullrequest.key=dispatch-feature-x -Dsonar.pullrequest.branch=feature-x -Dsonar.pullrequest.base=dev",
        ),
        (
            "a/b/c",
            "-Dsonar.pullrequest.key=dispatch-a-b-c -Dsonar.pullrequest.branch=a/b/c -Dsonar.pullrequest.base=dev",
        ),
    ],
)
def test_dispatched_runs_are_analysed_as_a_pull_request_into_dev(tmp_path, ref_name, args):
    steps = _workflow()["jobs"]["sonar"]["steps"]
    step = next(s for s in steps if s.get("id") == "dispatched")
    scan = next(s for s in steps if s.get("name") == "SonarQube Scan")
    assert step["if"] == "github.event_name == 'workflow_dispatch' && github.ref_name != 'dev'"
    assert step["env"] == {"REF_NAME": "${{ github.ref_name }}"}
    assert steps.index(step) < steps.index(scan)
    output = tmp_path / "output"
    output.write_text("")
    env = dict(os.environ, GITHUB_OUTPUT=str(output), REF_NAME=ref_name)
    result = subprocess.run(["bash", "-eo", "pipefail", "-c", step["run"]], env=env, capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr
    assert output.read_text() == f"args={args}\n"


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
