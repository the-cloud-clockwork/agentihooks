from pathlib import Path

import yaml

_ROOT = Path(__file__).resolve().parents[1]


def _workflow():
    return yaml.safe_load((_ROOT / ".github/workflows/proofs.yml").read_text())


def test_proofs_dispatch_cannot_satisfy_required_tests():
    workflow = _workflow()
    assert set(workflow[True]) == {"workflow_dispatch", "push"}
    assert workflow[True]["push"] == {"branches": ["diffcheck/**"], "paths": [".github/workflows/proofs.yml"]}
    assert workflow["concurrency"] == {"group": "proofs-${{ github.run_id }}", "cancel-in-progress": False}
    assert workflow["permissions"] == {"contents": "read", "pull-requests": "read"}
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
