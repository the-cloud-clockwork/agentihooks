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


def test_the_download_is_tried_three_times_and_only_the_last_failure_is_red():
    steps = _action()["runs"]["steps"]
    downloads = [s for s in steps if s.get("uses", "").startswith("actions/download-artifact@")]
    assert len(downloads) == 3
    assert [s.get("continue-on-error", False) for s in downloads] == [True, True, False]
    assert "if" not in downloads[0]
    for before, after in zip(downloads, downloads[1:]):
        assert after["if"] == f"steps.{before['id']}.outcome == 'failure'"
    for download in downloads:
        assert download["with"] == {"name": "${{ inputs.name }}", "path": "${{ inputs.path || github.workspace }}"}


def test_every_absorbed_failure_waits_and_warns_before_the_next_attempt():
    steps = _action()["runs"]["steps"]
    downloads = [i for i, s in enumerate(steps) if s.get("uses", "").startswith("actions/download-artifact@")]
    for before, after, wait in zip(downloads, downloads[1:], ("sleep 10", "sleep 20")):
        waits = steps[before + 1 : after]
        assert len(waits) == 1
        assert waits[0]["if"] == steps[after]["if"]
        assert "::warning title=Retried download::" in waits[0]["run"]
        assert wait in waits[0]["run"]
        assert "${{" not in waits[0]["run"]


@pytest.mark.parametrize(
    "job, name, given",
    [
        ("split", "Download the queued run's dev durations", {"name": "dev-durations", "path": "~/dev-durations"}),
        ("unit", "Download the durations this run splits on", {"name": "split-${{ matrix.python-version }}"}),
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
    action = step.get("uses", "")
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
