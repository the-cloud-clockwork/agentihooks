from pathlib import Path

import pytest
import yaml

_ROOT = Path(__file__).resolve().parents[1]
_RETRY = "./.github/actions/retry-download"
pytestmark = pytest.mark.unit


def _action():
    return yaml.safe_load((_ROOT / ".github/actions/retry-download/action.yml").read_text())


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
    for before, after in zip(downloads, downloads[1:]):
        waits = steps[before + 1 : after]
        assert len(waits) == 1
        assert waits[0]["if"] == steps[after]["if"]
        assert "::warning title=Retried download::" in waits[0]["run"]
        assert "sleep " in waits[0]["run"]
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


def _deploys(workflow, job):
    granted = {**(workflow.get("permissions") or {}), **(job.get("permissions") or {})}
    return "environment" in job or granted.get("id-token") == "write"


def test_no_deploying_job_retries():
    deploying = 0
    for path in sorted((_ROOT / ".github/workflows").glob("*.yml")):
        workflow = yaml.safe_load(path.read_text())
        for name, job in workflow["jobs"].items():
            if not _deploys(workflow, job):
                continue
            deploying += 1
            assert all(step.get("uses") != _RETRY for step in job.get("steps", [])), f"{path.name} {name}"
    assert deploying >= 3
