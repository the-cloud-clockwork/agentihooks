from pathlib import Path

import pytest
import yaml

WORKFLOWS = Path(__file__).resolve().parents[1] / ".github" / "workflows"

TRUSTED = (
    "(github.event_name != 'pull_request_target'"
    " && (github.event_name != 'pull_request' || github.event.pull_request.head.repo.full_name == github.repository)"
    ' && contains(fromJSON(\'["nestorcolt","agenticore-app[bot]","tcc-main-ci[bot]"]\'), github.actor)'
    ' && contains(fromJSON(\'["nestorcolt","agenticore-app[bot]","tcc-main-ci[bot]"]\'), github.triggering_actor)'
    " || (github.event_name == 'merge_group' || github.event_name == 'push' && github.ref == 'refs/heads/dev')"
    " && github.actor == 'github-merge-queue[bot]' && github.triggering_actor == 'github-merge-queue[bot]')"
)


def pool(label):
    return "${{ " + TRUSTED + " && '" + label + "' || 'ubuntu-latest' }}"


MOVED = {
    ("access-audit.yml", "audit"): "anton-oss-light",
    ("swarm-image.yml", "publish"): "anton-oss",
    ("mutation-preflight.yml", "mutation"): "anton-oss-light",
    ("mutation-preflight.yml", "mutation-shard"): "anton-oss-batch",
    ("mutation-preflight.yml", "plan"): "anton-oss-light",
    ("mutation-preflight.yml", "pull-request"): "anton-oss-light",
    ("mutation-preflight.yml", "stats"): "anton-oss-batch",
    ("test.yml", "admission"): "anton-oss-light",
    ("test.yml", "coverage-baseline"): "anton-oss-light",
    ("test.yml", "coverage-ratchet"): "anton-oss",
    ("test.yml", "durations"): "anton-oss-light",
    ("test.yml", "flake-budget"): "anton-oss-light",
    ("test.yml", "kind-due"): "anton-oss-light",
    ("test.yml", "mutation"): "anton-oss-batch",
    ("test.yml", "mutation-plan"): "anton-oss-light",
    ("test.yml", "mutation-stats"): "anton-oss-batch",
    ("test.yml", "queue-baseline"): "anton-oss",
    ("test.yml", "refresh-durations"): "anton-oss",
    ("test.yml", "shard-check"): "anton-oss-light",
    ("test.yml", "split"): "anton-oss",
    ("test.yml", "stage-budget"): "anton-oss-light",
    ("test.yml", "test-count"): "anton-oss-light",
    ("test.yml", "unit"): "anton-oss",
}


def jobs(name):
    return yaml.safe_load((WORKFLOWS / name).read_text())["jobs"]


@pytest.mark.parametrize(("workflow", "job"), sorted(MOVED))
def test_heavy_jobs_run_on_the_isolated_anton_pool_only_for_trusted_actors(workflow, job):
    assert jobs(workflow)[job]["runs-on"] == pool(MOVED[(workflow, job)])


def test_no_workflow_reaches_an_anton_pool_except_through_the_trusted_expression():
    for path in sorted(WORKFLOWS.glob("*.yml")):
        for name, job in yaml.safe_load(path.read_text())["jobs"].items():
            runs_on = str(job.get("runs-on", ""))
            if "anton" in runs_on:
                assert (path.name, name) in MOVED, f"{path.name}:{name}"


def test_pull_request_target_workflows_stay_on_hosted_runners():
    for path in sorted(WORKFLOWS.glob("*.yml")):
        workflow = yaml.safe_load(path.read_text())
        events = workflow.get("on", workflow.get(True, {}))
        if "pull_request_target" in ([events] if isinstance(events, str) else events):
            assert "anton" not in path.read_text(), path.name
