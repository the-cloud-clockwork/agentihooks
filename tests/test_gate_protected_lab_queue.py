import copy
from typing import Any

import pytest

from scripts import gate_protected
from tests.test_gate_protected import _lab_aggregators

pytestmark = [pytest.mark.unit, pytest.mark.feature("lab-verdict-merge-group-rendering")]

_ERROR = "The head's lab verdict job differs from the reviewed rendering; the protected branch grades with its own."


def _job() -> dict[str, Any]:
    return {
        "if": "github.event_name == 'pull_request'",
        "runs-on": "ubuntu-latest",
        "permissions": {"contents": "read"},
        "outputs": {"authoritative": "${{ steps.authority.outputs.authoritative }}"},
        "steps": [
            {
                "uses": "actions/checkout@v4",
                "with": {"ref": "${{ github.event.pull_request.base.sha }}", "path": "grader"},
            },
            {
                "id": "authority",
                "env": {"AGENTIHOOKS_LAB_OFF": "${{ vars.AGENTIHOOKS_LAB_OFF }}"},
                "run": "python -m scripts.ci_lab_verdict authority . pull_request",
            },
            {
                "id": "app-token",
                "uses": "actions/create-github-app-token@v3.2.0",
                "with": {"permission-checks": "read"},
            },
            {
                "id": "verdict",
                "env": {"HEAD": "${{ github.event.pull_request.head.sha }}"},
                "run": 'python -m scripts.ci_lab_verdict verdict . "$AUTHORITATIVE" "$GITHUB_REPOSITORY" "$HEAD" "$LAB_APP"',
            },
        ],
    }


def _queue(job: dict[str, Any]) -> dict[str, Any]:
    result = copy.deepcopy(job)
    result["if"] = "github.event_name == 'pull_request' || github.event_name == 'merge_group'"
    result["steps"][0]["with"]["ref"] = "${{ github.event.pull_request.base.sha || github.event.merge_group.base_sha }}"
    result["steps"][1]["env"]["EVENT"] = "${{ github.event_name }}"
    result["steps"][1]["run"] = 'python -m scripts.ci_lab_verdict authority . "$EVENT"'
    result["steps"][3]["env"]["HEAD"] = "${{ github.event.pull_request.head.sha || github.event.merge_group.head_sha }}"
    return result


def _problems(base: dict[str, Any], head: dict[str, Any]) -> list[str]:
    base_workflow, head_workflow = _lab_aggregators()
    base_workflow["jobs"]["lab-verdict"] = base
    head_workflow["jobs"]["lab-verdict"] = head
    return gate_protected.grade({"test.yml": base_workflow}, {}, {"test.yml": head_workflow}, {})


def test_the_exact_merge_group_lab_rendering_is_accepted_without_changing_the_base() -> None:
    base = _job()
    before = copy.deepcopy(base)
    assert _problems(base, _queue(base)) == []
    assert base == before


@pytest.mark.parametrize("queued", [False, True])
def test_the_unchanged_lab_rendering_stays_pinned(queued: bool) -> None:
    job = _queue(_job()) if queued else _job()
    assert _problems(job, copy.deepcopy(job)) == []
    if queued:
        assert _problems(job, _job()) == [_ERROR]


@pytest.mark.parametrize("part", ["condition", "checkout", "authority", "head"])
def test_a_partial_merge_group_lab_rendering_is_refused(part: str) -> None:
    base = _job()
    head = _queue(base)
    if part == "condition":
        head["if"] = base["if"]
    elif part == "checkout":
        head["steps"][0] = copy.deepcopy(base["steps"][0])
    elif part == "authority":
        head["steps"][1] = copy.deepcopy(base["steps"][1])
    else:
        head["steps"][3] = copy.deepcopy(base["steps"][3])
    assert _problems(base, head) == [_ERROR]


@pytest.mark.parametrize("part", ["runner", "permissions", "outputs", "token", "command", "extra", "event"])
@pytest.mark.parametrize("queued_base", [False, True])
def test_the_merge_group_rendering_cannot_change_other_job_fields(part: str, queued_base: bool) -> None:
    base = _queue(_job()) if queued_base else _job()
    head = _queue(_job())
    if part == "runner":
        head["runs-on"] = "unreviewed"
    elif part == "permissions":
        head["permissions"]["contents"] = "write"
    elif part == "outputs":
        head["outputs"]["authoritative"] = "{}"
    elif part == "token":
        head["steps"][2]["with"]["permission-checks"] = "write"
    elif part == "command":
        head["steps"][3]["run"] = "true"
    elif part == "extra":
        head["steps"].append({"run": "true"})
    else:
        head["steps"][1]["env"]["EVENT"] = "pull_request"
    assert _problems(base, head) == [_ERROR]


@pytest.mark.parametrize("part", ["missing", "condition", "checkout", "authority", "head", "duplicate"])
def test_an_unreviewed_base_cannot_gain_a_queue_rendering(part: str) -> None:
    base = _job()
    if part == "missing":
        base.pop("steps")
    elif part == "condition":
        base["if"] = "false"
    elif part == "checkout":
        base["steps"][0]["with"]["ref"] = "unreviewed"
    elif part == "authority":
        base["steps"][1]["run"] = "true"
    elif part == "head":
        base["steps"][3]["env"]["HEAD"] = "unreviewed"
    else:
        base["steps"].append(copy.deepcopy(base["steps"][0]))
    head = _queue(base) if part != "missing" else base | {"if": "github.event_name == 'merge_group'"}
    assert _problems(base, head) == [_ERROR]
