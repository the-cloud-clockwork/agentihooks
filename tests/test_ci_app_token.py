import re
from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.unit

_WORKFLOW = Path(__file__).parent.parent / ".github/workflows/test.yml"
_APP_TOKEN = "${{ steps.app-token.outputs.token }}"
_BUCKETS = "Report the API rate limit buckets"


def _jobs():
    return yaml.safe_load(_WORKFLOW.read_text())["jobs"]


def _calls_api(step):
    run = step.get("run", "")
    return "GH_TOKEN" in step.get("env", {}) or re.search(r"(^|[\s;(|&$])gh (api|run|pr)\b", run) is not None


def _consumers():
    return [
        (name, job, step)
        for name, job in _jobs().items()
        for step in job.get("steps", [])
        if _calls_api(step) and step.get("name") != _BUCKETS
    ]


def test_tests_workflow_has_api_consumers():
    assert {name for name, _, _ in _consumers()} >= {"unit", "shard-check", "sonar", "refresh-durations", "record-pass"}


@pytest.mark.parametrize("name,job,step", _consumers(), ids=lambda v: v if isinstance(v, str) else "")
def test_every_api_step_uses_the_minted_app_token(name, job, step):
    assert step["env"]["GH_TOKEN"] == _APP_TOKEN, (name, step.get("name"))
    steps = job["steps"]
    mint = next(s for s in steps if s.get("id") == "app-token")
    assert steps.index(mint) < steps.index(step)
    assert mint.get("if") in (None, step.get("if")), (name, step.get("name"))


@pytest.mark.parametrize("name", sorted({name for name, _, _ in _consumers()}))
def test_each_consuming_job_mints_a_read_only_repository_token(name):
    mint = next(s for s in _jobs()[name]["steps"] if s.get("id") == "app-token")
    assert mint["uses"] == "actions/create-github-app-token@v3.2.0"
    assert mint["with"] == {
        "app-id": "${{ secrets.TESTS_APP_ID }}",
        "private-key": "${{ secrets.TESTS_APP_PRIVATE_KEY }}",
        "permission-actions": "read",
        "permission-contents": "read",
        "permission-pull-requests": "read",
    }
    assert "continue-on-error" not in mint


def test_no_step_falls_back_to_the_workflow_token():
    for name, job in _jobs().items():
        for step in job.get("steps", []):
            if step.get("name") == _BUCKETS:
                continue
            assert "github.token" not in str(step) and "GITHUB_TOKEN" not in str(step), (name, step.get("name"))


def test_sonar_reports_both_rate_limit_buckets_without_spending_either():
    steps = _jobs()["sonar"]["steps"]
    report = next(s for s in steps if s.get("name") == _BUCKETS)
    assert report["env"] == {"APP_TOKEN": _APP_TOKEN, "WORKFLOW_TOKEN": "${{ github.token }}"}
    assert set(re.findall(r"gh api (\S+)", report["run"])) == {"rate_limit"}
    assert steps.index(next(s for s in steps if s.get("id") == "app-token")) < steps.index(report)
