import os
import subprocess
import time
import zipfile
from pathlib import Path

import pytest
import yaml

_ROOT = Path(__file__).resolve().parents[1]
_RETRY = "./.github/actions/retry-download"
_RETRY_UPLOAD = "./.github/actions/retry-upload"
pytestmark = pytest.mark.unit


def _action():
    return yaml.safe_load((_ROOT / ".github/actions/retry-download/action.yml").read_text())


def _upload_action():
    return yaml.safe_load((_ROOT / ".github/actions/retry-upload/action.yml").read_text())


def _steps(job):
    return yaml.safe_load((_ROOT / ".github/workflows/test.yml").read_text())["jobs"][job]["steps"]


_APP = {"client-id": "${{ secrets.TCC_CI_CLIENT_ID }}", "private-key": "${{ secrets.TCC_CI_APP_PRIVATE_KEY }}"}


def _fallback():
    return next(s for s in _action()["runs"]["steps"] if s.get("name") == "Download through the REST artifacts API")


def test_one_download_then_a_rest_fallback_only_when_it_fails():
    primary, mint, fallback = _action()["runs"]["steps"]
    assert primary["uses"] == "actions/download-artifact@v4"
    assert primary["continue-on-error"] is True and "if" not in primary
    assert primary["with"] == {"name": "${{ inputs.name }}", "path": "${{ inputs.path || github.workspace }}"}
    assert mint["uses"] == "actions/create-github-app-token@v3.2.0"
    assert mint["if"].startswith("steps.primary.outcome == 'failure'")
    assert mint["with"]["permission-actions"] == "read"
    assert fallback["if"] == "steps.primary.outcome == 'failure'"
    assert fallback["env"]["GH_TOKEN"] == "${{ steps.app-token.outputs.token }}"
    assert "continue-on-error" not in fallback
    assert "actions/artifacts/$id/zip" in fallback["run"]
    assert "${{" not in fallback["run"]


def _run_fallback(tmp_path, gh, token="app-token", attempt="1"):
    tools = tmp_path / "bin"
    tools.mkdir(exist_ok=True)
    (tools / "gh").write_text(f'#!/usr/bin/env bash\necho "$*" >> {tmp_path}/calls\n{gh}\n')
    (tools / "gh").chmod(0o755)
    env = {
        **os.environ,
        **_fallback()["env"],
        "PATH": f"{tools}:{os.environ['PATH']}",
        "GH_TOKEN": token,
        "ARTIFACT": "split-3.12",
        "HOME": str(tmp_path),
        "DEST": "~/out",
        "ATTEMPT_SECONDS": attempt,
        "WAIT_SECONDS": "0",
        "GITHUB_REPOSITORY": "o/r",
        "GITHUB_RUN_ID": "7",
    }
    started = time.monotonic()
    done = subprocess.run(
        ["bash", "--noprofile", "--norc", "-eo", "pipefail", "-c", _fallback()["run"]],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    return done, time.monotonic() - started


@pytest.mark.wall_clock
def test_a_stalled_rest_attempt_ends_within_its_timeout(tmp_path):
    done, elapsed = _run_fallback(tmp_path, "sleep 300")
    assert done.returncode == 1
    assert done.stdout.count("::warning title=Retried download::REST attempt") == 3
    assert "exceeded 1s" in done.stdout
    assert elapsed < 15


def test_the_rest_fallback_fetches_the_artifact_by_id(tmp_path):
    archive = tmp_path / "artifact.zip"
    with zipfile.ZipFile(archive, "w") as packed:
        packed.writestr(".test_durations", "{}")
    gh = f'case "$*" in *"/artifacts?name="*) echo 42 ;; *"/artifacts/42/zip"*) cat {archive} ;; *) exit 1 ;; esac'
    done, _ = _run_fallback(tmp_path, gh, attempt="30")
    assert done.returncode == 0, done.stdout + done.stderr
    assert (tmp_path / "out/.test_durations").read_text() == "{}"
    calls = (tmp_path / "calls").read_text().splitlines()
    assert calls[0].startswith("api repos/o/r/actions/runs/7/artifacts?name=split-3.12&per_page=100 --jq")
    assert calls[1] == "api repos/o/r/actions/artifacts/42/zip"


def test_the_rest_fallback_without_an_app_token_stays_red(tmp_path):
    done, _ = _run_fallback(tmp_path, "exit 0", token="")
    assert done.returncode == 1
    assert "no App token was minted" in done.stdout
    assert not (tmp_path / "calls").exists()


@pytest.mark.parametrize(
    "job, name, given",
    [
        (
            "split",
            "Download the queued run's dev durations",
            {"name": "dev-durations", "path": "~/dev-durations", **_APP},
        ),
        ("unit", "Download the durations this run splits on", {"name": "split-${{ matrix.python-version }}", **_APP}),
    ],
)
def test_the_durations_downloads_retry_in_place(job, name, given):
    step = next(s for s in _steps(job) if s.get("name") == name)
    assert step["uses"] == _RETRY
    assert step["with"] == given


_UPLOAD_WITH = {
    "name": "${{ inputs.name }}",
    "path": "${{ inputs.path }}",
    "include-hidden-files": "${{ inputs.include-hidden-files }}",
    "if-no-files-found": "${{ inputs.if-no-files-found }}",
    "retention-days": "${{ inputs.retention-days }}",
    "overwrite": True,
}


def test_the_upload_is_tried_three_times_overwriting_and_only_the_last_failure_is_red():
    steps = _upload_action()["runs"]["steps"]
    uploads = [s for s in steps if s.get("uses", "").startswith("actions/upload-artifact@")]
    assert len(uploads) == 3
    assert [s.get("continue-on-error", False) for s in uploads] == [True, True, False]
    assert "if" not in uploads[0]
    for before, after in zip(uploads, uploads[1:]):
        assert after["if"] == f"steps.{before['id']}.outcome == 'failure'"
    for upload in uploads:
        assert upload["with"] == _UPLOAD_WITH


def test_every_absorbed_upload_failure_waits_and_warns_before_the_next_attempt():
    steps = _upload_action()["runs"]["steps"]
    uploads = [i for i, s in enumerate(steps) if s.get("uses", "").startswith("actions/upload-artifact@")]
    for before, after, wait in zip(uploads, uploads[1:], ("sleep 10", "sleep 20")):
        waits = steps[before + 1 : after]
        assert len(waits) == 1
        assert waits[0]["if"] == steps[after]["if"]
        assert "::warning title=Retried upload::" in waits[0]["run"]
        assert wait in waits[0]["run"]
        assert "${{" not in waits[0]["run"]


def test_a_missing_file_stays_red_by_default_only_where_the_caller_asks():
    inputs = _upload_action()["inputs"]
    assert inputs["if-no-files-found"]["default"] == "warn"
    assert inputs["include-hidden-files"]["default"] == "false"
    assert all(inputs[name]["required"] for name in ("name", "path", "retention-days"))


@pytest.mark.parametrize(
    "job, name, given",
    [
        (
            "split",
            "Upload the chosen durations",
            {
                "name": "split-${{ matrix.python-version }}",
                "path": ".test_durations\ndurations.sha256\n",
                "include-hidden-files": True,
                "if-no-files-found": "error",
                "retention-days": 7,
            },
        ),
        (
            "unit",
            "Upload durations",
            {
                "name": "durations-${{ matrix.python-version }}-${{ matrix.shard }}",
                "path": "durations.json\ndurations.sha256\nelapsed.json\n",
                "retention-days": 14,
            },
        ),
        (
            "unit",
            "Upload the flake report",
            {
                "name": "flakes-${{ matrix.python-version }}-${{ matrix.shard }}",
                "path": "flakes.jsonl",
                "if-no-files-found": "error",
                "retention-days": 14,
            },
        ),
        (
            "refresh-durations",
            "Publish merged durations",
            {
                "name": "durations-merged",
                "path": ".test_durations*",
                "include-hidden-files": True,
                "retention-days": 14,
            },
        ),
    ],
)
def test_the_durations_uploads_retry_in_place(job, name, given):
    step = next(s for s in _steps(job) if s.get("name") == name)
    assert step["uses"] == _RETRY_UPLOAD
    assert step["with"] == given


def _deploys(workflow, job):
    granted = job.get("permissions", workflow.get("permissions")) or {}
    oidc = granted == "write-all" or (isinstance(granted, dict) and granted.get("id-token") == "write")
    return "environment" in job or oidc


def _retries(job):
    called = job.get("uses", "")
    if called.startswith("./.github/workflows/"):
        return any(_retries(inner) for inner in yaml.safe_load((_ROOT / called).read_text())["jobs"].values())
    return any(_step_retries(step) for step in job.get("steps", []))


def _step_retries(step):
    action = step.get("uses", "").replace("./.workflow-revision/", "./", 1)
    if action.rstrip("/") == _RETRY:
        return True
    if not action.startswith("./.github/actions/"):
        return False
    return any(
        _step_retries(inner) for inner in yaml.safe_load((_ROOT / action / "action.yml").read_text())["runs"]["steps"]
    )


def test_no_job_with_an_environment_or_oidc_grant_retries():
    deploying = 0
    for path in sorted((_ROOT / ".github/workflows").glob("*.y*ml")):
        workflow = yaml.safe_load(path.read_text())
        for name, job in workflow["jobs"].items():
            if _deploys(workflow, job):
                deploying += 1
                assert not _retries(job), f"{path.name} {name}"
    assert deploying >= 3


def test_the_guard_sees_a_retry_behind_a_reusable_workflow_or_a_composite_action():
    assert _retries({"uses": "./.github/workflows/test.yml"})
    assert _step_retries({"uses": f"{_RETRY}/"})
    assert not _retries({"steps": [{"uses": "./.github/actions/browser-cache"}]})
    assert _deploys({"permissions": "write-all"}, {})
    assert _deploys({}, {"environment": "pypi"})
    assert _deploys({}, {"permissions": {"id-token": "write"}})
    assert _deploys({"permissions": {"id-token": "write"}}, {})
    assert not _deploys({"permissions": {"id-token": "write"}}, {"permissions": {}})
    assert not _deploys({"permissions": {"id-token": "write"}}, {"permissions": {"contents": "read"}})
