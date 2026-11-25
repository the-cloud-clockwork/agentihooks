"""Tests for the Lab check workflow: its dispatch trigger, token scope, queue per head and the entry it runs."""

from pathlib import Path
from typing import Any

import pytest
import yaml

from scripts import ci_branch_lanes
from scripts.ci_lab import checks

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[1]


def workflow() -> dict[Any, Any]:
    """Return the Lab check workflow as parsed YAML."""
    loaded: dict[Any, Any] = yaml.safe_load((ROOT / ".github/workflows/lab-check.yml").read_text())
    return loaded


@pytest.mark.feature("lab-check-workflow")
def test_the_lab_check_workflow_runs_only_on_the_lab_check_dispatch() -> None:
    assert workflow()[True] == {"repository_dispatch": {"types": [checks.EVENT]}}


@pytest.mark.feature("lab-check-workflow")
def test_the_post_job_alone_holds_checks_write_beside_contents_read() -> None:
    parsed = workflow()
    assert parsed["permissions"] == {"contents": "read"}
    assert list(parsed["jobs"]) == ["post"]
    assert parsed["jobs"]["post"]["permissions"] == {"checks": "write", "contents": "read"}


@pytest.mark.feature("lab-check-workflow")
def test_runs_for_one_head_queue_behind_each_other_and_none_is_cancelled() -> None:
    assert workflow()["concurrency"] == {
        "group": "lab-check-${{ github.event.client_payload.sha }}",
        "queue": "max",
        "cancel-in-progress": False,
    }


@pytest.mark.feature("lab-check-workflow")
def test_the_post_job_waits_on_the_shared_hosted_lane_and_the_branch_lane_gate_passes_it() -> None:
    assert workflow()["jobs"]["post"]["concurrency"] == {
        "group": ci_branch_lanes.GROUP,
        "queue": "max",
        "cancel-in-progress": False,
    }
    assert ci_branch_lanes.grade((ROOT / ".github/workflows/lab-check.yml").read_text()) == []


@pytest.mark.feature("lab-check-workflow")
def test_the_post_step_runs_the_poster_entry_with_the_workflow_token() -> None:
    steps = workflow()["jobs"]["post"]["steps"]
    assert steps[0]["with"] == {"persist-credentials": False}
    assert steps[-1]["run"] == "python -m scripts.ci_lab.checks"
    assert steps[-1]["env"] == {"GITHUB_TOKEN": "${{ github.token }}"}
