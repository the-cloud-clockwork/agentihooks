from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.unit

_ROOT = Path(__file__).resolve().parents[1]
_WORKFLOWS = _ROOT / ".github/workflows"

LANE = (
    "format('hosted-lane-{0}', "
    "(endsWith(github.run_id, '0') || endsWith(github.run_id, '1') || endsWith(github.run_id, '2')) && 'a' || "
    "(endsWith(github.run_id, '3') || endsWith(github.run_id, '4') || endsWith(github.run_id, '5')) && 'b' || 'c')"
)


def _workflow(name):
    return yaml.safe_load((_WORKFLOWS / name).read_text())


def _direct(workflow, own_group):
    title = workflow["name"]
    return {
        "group": f"${{{{ github.workflow == '{title}' && {LANE} || format('{own_group}-{{0}}', github.run_id) }}}}",
        "queue": f"${{{{ github.workflow == '{title}' && 'max' || 'single' }}}}",
    }


def test_test_dispatches_queue_on_the_shared_lanes_and_required_events_keep_their_own_group():
    concurrency = _workflow("test.yml")["concurrency"]
    assert concurrency == {
        "group": (
            f"${{{{ github.event_name == 'workflow_dispatch' && {LANE} || "
            "format('tests-{0}-{1}', github.event_name, github.event.pull_request.number || github.run_id) }}"
        ),
        "queue": "${{ github.event_name == 'workflow_dispatch' && 'max' || 'single' }}",
        "cancel-in-progress": "${{ github.event_name == 'pull_request' }}",
    }


def test_proofs_queue_on_the_shared_lanes_without_cancelling():
    assert _workflow("proofs.yml")["concurrency"] == {
        "group": f"${{{{ {LANE} }}}}",
        "queue": "max",
        "cancel-in-progress": False,
    }


@pytest.mark.parametrize(("name", "own_group"), [("helm-kind.yml", "helm-kind"), ("ledger-load.yml", "ledger-load")])
def test_direct_proof_runs_queue_on_the_lanes_and_calls_from_tests_keep_a_per_run_group(name, own_group):
    workflow = _workflow(name)
    assert "workflow_call" in workflow[True]
    assert workflow["concurrency"] == _direct(workflow, own_group)


def test_worker_image_queues_branch_runs_on_the_lanes_and_keeps_dev_pushes_uncapped():
    assert _workflow("swarm-node-image.yml")["concurrency"] == {
        "group": f"${{{{ github.ref == 'refs/heads/dev' && format('swarm-node-image-{{0}}', github.ref) || {LANE} }}}}",
        "queue": "${{ github.ref == 'refs/heads/dev' && 'single' || 'max' }}",
        "cancel-in-progress": False,
    }


def test_branch_mutation_queues_on_the_lanes_and_called_mutation_keeps_a_per_run_group():
    workflow = _workflow("mutation-preflight.yml")
    branch = "github.event_name == 'push' && github.ref != 'refs/heads/dev'"
    assert workflow["jobs"]["mutation"]["concurrency"] == {
        "group": f"${{{{ {branch} && {LANE} || format('mutation-preflight-job-{{0}}', github.run_id) }}}}",
        "queue": f"${{{{ {branch} && 'max' || 'single' }}}}",
    }
    assert "mutation-preflight-job-" not in workflow["concurrency"]["group"]


def _concurrency_blocks():
    for path in sorted(_WORKFLOWS.glob("*.yml")):
        workflow = yaml.safe_load(path.read_text())
        if "concurrency" in workflow:
            yield path.name, workflow["concurrency"]
        for key, job in workflow.get("jobs", {}).items():
            if isinstance(job, dict) and "concurrency" in job:
                yield f"{path.name}:{key}", job["concurrency"]


def test_every_lane_user_names_the_same_three_lanes_and_never_cancels():
    users = {where: block for where, block in _concurrency_blocks() if "hosted-lane" in str(block["group"])}
    assert set(users) == {
        "test.yml",
        "proofs.yml",
        "helm-kind.yml",
        "ledger-load.yml",
        "swarm-node-image.yml",
        "mutation-preflight.yml:mutation",
    }
    for where, block in users.items():
        assert str(block["group"]).count("hosted-lane") == 1, where
        assert LANE in block["group"], where
        assert block.get("cancel-in-progress") in (None, False, "${{ github.event_name == 'pull_request' }}"), where
        assert "max" in str(block["queue"]), where


def test_required_events_never_reach_a_lane():
    group = _workflow("test.yml")["concurrency"]["group"]
    assert group.startswith("${{ github.event_name == 'workflow_dispatch' && ")
    required = _workflow("test.yml")[True]
    assert {"pull_request", "push", "merge_group"} <= set(required)
    assert required["push"] == {"branches": ["dev"]}
