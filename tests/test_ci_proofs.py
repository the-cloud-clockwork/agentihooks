import os
import subprocess
from pathlib import Path

import pytest
import yaml

_ROOT = Path(__file__).resolve().parents[1]


def _workflow():
    return yaml.safe_load((_ROOT / ".github/workflows/proofs.yml").read_text())


def test_proofs_dispatch_cannot_satisfy_required_tests():
    workflow = _workflow()
    assert set(workflow[True]) == {"workflow_dispatch"}
    assert workflow["concurrency"]["group"].startswith(
        "${{ inputs.proof == 'tests' && format('focused-proof-{0}', github.run_id) || format('hosted-lane-{0}', "
    )
    assert (workflow["concurrency"]["queue"], workflow["concurrency"]["cancel-in-progress"]) == ("max", False)
    assert workflow["permissions"] == {"contents": "read", "pull-requests": "read", "actions": "read"}
    inputs = workflow[True]["workflow_dispatch"]["inputs"]
    assert inputs["proof"]["type"] == "choice"
    assert inputs["proof"]["required"] is True
    assert set(inputs["proof"]["options"]) == set(workflow["jobs"])
    assert "gate plant proofs must use the full Tests dispatch" in inputs["proof"]["description"]
    assert all(job.get("name", key) != "Gate — Required" for key, job in workflow["jobs"].items())
    required = yaml.safe_load((_ROOT / ".github/workflows/test.yml").read_text())
    assert {"pull_request", "push", "merge_group", "workflow_dispatch"} <= set(required[True])
    assert required["jobs"]["gate-required"]["name"] == "Gate — Required"
    assert "proof" not in required[True]["workflow_dispatch"]["inputs"]


@pytest.mark.parametrize(
    ("paths", "code"),
    [
        ("tests/test_a.py tests/test_b.py::test_c", 0),
        ("", 1),
        ("tests/test_a.py\ntests/test_b.py", 1),
        ("tests/../hooks", 1),
        ("hooks/test_a.py", 1),
    ],
)
def test_a_focused_proof_is_red_unless_every_test_path_is_on_one_line_under_tests(paths, code):
    step = next(s for s in _workflow()["jobs"]["tests"]["steps"] if s.get("name") == "Require explicit test paths")
    result = subprocess.run(
        ["bash", "-e", "-c", step["run"]], env={**os.environ, "TEST_PATHS": paths}, capture_output=True
    )
    assert result.returncode == code, result.stdout + result.stderr


def test_proofs_reuse_selected_mutation_and_smoke_execution():
    workflow = _workflow()
    jobs = workflow["jobs"]
    for proof, reusable in (
        ("mutation", "mutation-preflight"),
        ("semgrep", "semgrep"),
        ("worker-image", "swarm-node-smoke"),
        ("swarm-image", "swarm-smoke"),
        ("brain-smoke", "brain-smoke"),
        ("helm-kind", "helm-kind"),
    ):
        job = jobs[proof]
        assert job["uses"] == f"./.github/workflows/{reusable}.yml"
        assert job["if"] == f"inputs.proof == '{proof}'"
        assert "steps" not in job
        assert "needs" not in job
        assert not job.get("continue-on-error", False)
        if proof in {"mutation", "semgrep"}:
            assert job["with"] == {"base": "${{ inputs.base }}"}
    callable = yaml.safe_load((_ROOT / ".github/workflows/mutation-preflight.yml").read_text())
    assert callable[True]["workflow_call"]["inputs"]["base"] == {"type": "string", "required": True}
