from pathlib import Path

import pytest
import yaml

from scripts import ci_branch_lanes

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[1]
ROUTED = "${{ contains(fromJSON('[\"nestorcolt\"]'), github.actor) && '%s' || 'ubuntu-latest' }}"


@pytest.mark.parametrize(
    ("runner", "label"),
    [
        ("anton-light", "anton-light"),
        (["self-hosted", "anton-light"], "self-hosted, anton-light"),
        (ROUTED % "anton-light", "anton-light"),
        ({"group": "Default", "labels": ["anton-light"]}, "anton-light"),
        ("anton-heavy", "anton-heavy"),
        ("Anton-Light", "Anton-Light"),
        ("gpu-light", "gpu-light"),
        ({"group": "Default"}, "group Default"),
        ({"group": "anton-oss", "labels": "anton-light"}, "anton-light"),
        ("${{ matrix.runner }}", "${{ matrix.runner }}"),
        ("${{ format('anton-{0}', 'light') }}", "${{ format('anton-{0}', 'light') }}"),
        (
            "${{ github.event_name == 'push' && matrix.runner || 'anton-oss' }}",
            "${{ github.event_name == 'push' && matrix.runner || 'anton-oss' }}",
        ),
        (
            "${{ github.event_name == 'push' && 'anton-oss' || inputs.runner }}",
            "${{ github.event_name == 'push' && 'anton-oss' || inputs.runner }}",
        ),
        (
            "${{ format('{0}', 'anton-light') || 'anton-oss' }}",
            "${{ format('{0}', 'anton-light') || 'anton-oss' }}",
        ),
        ("${{ github.event_name == 'push' && 'anton-light' || 'anton-oss' }}", "anton-light"),
        ("${{ inputs.label || 'ubuntu-latest' }}", "${{ inputs.label || 'ubuntu-latest' }}"),
        ("${{ github.ref_name || 'anton-oss' }}", "${{ github.ref_name || 'anton-oss' }}"),
        ("${{ secrets.R || 'anton-oss' }}", "${{ secrets.R || 'anton-oss' }}"),
        ("${{ matrix['runner'] || 'anton-oss' }}", "${{ matrix['runner'] || 'anton-oss' }}"),
    ],
)
def test_a_private_runner_label_is_refused(runner, label):
    assert ", ".join(ci_branch_lanes.private_runners(runner)) == label


@pytest.mark.parametrize(
    "runner",
    [
        "ubuntu-latest",
        "ubuntu-24.04",
        "anton-oss",
        "anton-oss-light",
        "anton-oss-batch",
        "anton-oss-queue",
        ["anton-oss-light"],
        ROUTED % "anton-oss-light",
        "${{ github.event_name == 'merge_group' && 'anton-oss-queue' || 'anton-oss' }}",
        {"labels": ["anton-oss-batch"]},
        {"group": "anton-oss", "labels": "anton-oss"},
        "Ubuntu-Latest",
        "${{ 'anton-oss' }}",
        "windows-latest",
        "macos-14",
        None,
    ],
)
def test_public_runner_labels_pass(runner):
    assert ci_branch_lanes.private_runners(runner) == []


def test_problems_name_each_job_with_a_private_label():
    workflow = {
        "jobs": {
            "ok": {"runs-on": "anton-oss-light"},
            "bad": {"runs-on": ["anton-light", "anton-oss", "anton-heavy"]},
            "call": {"uses": "./.github/workflows/b.yml"},
        }
    }
    assert ci_branch_lanes.runner_problems(workflow) == [
        "job bad asks for private runner label anton-light, anton-heavy"
    ]
    assert ci_branch_lanes.runner_problems(None) == []
    assert ci_branch_lanes.runner_problems({"jobs": "x"}) == []


def test_the_branch_lanes_grader_refuses_a_planted_private_label():
    lane = {"group": ci_branch_lanes.GROUP, "queue": "max"}
    planted = {"name": "Proof", "on": {"push": {}}, "concurrency": lane, "jobs": {"reply": {"runs-on": "anton-light"}}}
    assert ci_branch_lanes.grade(yaml.safe_dump(planted)) == ["job reply asks for private runner label anton-light"]
    planted["jobs"]["reply"]["runs-on"] = "anton-oss-light"
    assert ci_branch_lanes.grade(yaml.safe_dump(planted)) == []


def test_the_repository_workflows_ask_only_for_public_runners():
    folder = ROOT / ".github/workflows"
    for path in sorted([*folder.glob("*.yml"), *folder.glob("*.yaml")]):
        assert ci_branch_lanes.runner_problems(yaml.safe_load(path.read_text())) == [], path.name
