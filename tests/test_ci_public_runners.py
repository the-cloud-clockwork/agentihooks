from pathlib import Path

import pytest
import yaml

from scripts import ci_public_runners

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[1]
ROUTED = "${{ contains(fromJSON('[\"nestorcolt\"]'), github.actor) && '%s' || 'ubuntu-latest' }}"


def workflow(runner):
    return {"b.yml": {"on": {"push": {}}, "jobs": {"build": {"runs-on": runner, "steps": []}}}}


@pytest.mark.parametrize(
    "runner",
    [
        "anton-light",
        ["self-hosted", "anton-light"],
        ROUTED % "anton-light",
        {"group": "Default", "labels": ["anton-light"]},
        "self-hosted",
        "anton-heavy",
    ],
)
def test_a_private_runner_label_is_refused(runner):
    problems = ci_public_runners.check(workflow(runner))
    assert len(problems) == 1
    assert problems[0].startswith("b.yml/build: runs-on ")


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
    ],
)
def test_public_runner_labels_pass(runner):
    assert ci_public_runners.check(workflow(runner)) == []


def test_reusable_workflow_calls_are_not_graded():
    assert ci_public_runners.check({"c.yml": {"jobs": {"call": {"uses": "./.github/workflows/b.yml"}}}}) == []


def test_every_refused_label_is_named():
    problems = ci_public_runners.check(workflow(["anton-light", "anton-oss", "anton-heavy"]))
    assert problems == ["b.yml/build: runs-on anton-light, anton-heavy is not a public runner label"]


def test_main_reports_a_planted_label(tmp_path, capsys):
    folder = tmp_path / ".github/workflows"
    folder.mkdir(parents=True)
    (folder / "ok.yml").write_text(yaml.safe_dump(workflow("anton-oss")["b.yml"]))
    assert ci_public_runners.main(["--root", str(tmp_path)]) == 0
    (folder / "bad.yaml").write_text(yaml.safe_dump(workflow("anton-light")["b.yml"]))
    assert ci_public_runners.main(["--root", str(tmp_path)]) == 1
    out = capsys.readouterr().out
    assert "::error::bad.yaml/build: runs-on anton-light is not a public runner label" in out
    assert "Public runner labels: 2 workflows, 1 problems" in out


def test_the_repository_workflows_ask_only_for_public_runners():
    assert ci_public_runners.main(["--root", str(ROOT)]) == 0


def test_every_branch_push_of_a_workflow_runs_the_check_on_a_hosted_runner():
    flow = yaml.safe_load((ROOT / ".github/workflows/public-runners.yml").read_text())
    on = flow.get("on", flow.get(True))
    assert on["push"]["paths"] == [".github/workflows/**"]
    assert "branches" not in on["push"]
    assert on["pull_request"]["paths"] == [".github/workflows/**"]
    (job,) = flow["jobs"].values()
    assert job["runs-on"] == "ubuntu-latest"
    assert any(step.get("run") == "python3 -m scripts.ci_public_runners" for step in job["steps"])
