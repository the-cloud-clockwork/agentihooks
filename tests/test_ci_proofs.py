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
def test_a_focused_proof_is_red_unless_every_test_path_is_on_one_line_under_tests(paths, code, tmp_path):
    step = next(s for s in _workflow()["jobs"]["tests"]["steps"] if s.get("name") == "Require explicit test paths")
    (tmp_path / "tests").mkdir()
    for name in ("test_a.py", "test_b.py"):
        (tmp_path / "tests" / name).touch()
    result = subprocess.run(
        ["bash", "-e", "-c", step["run"]],
        env={**os.environ, "TEST_PATHS": paths},
        cwd=tmp_path,
        capture_output=True,
    )
    assert result.returncode == code, result.stdout + result.stderr


@pytest.mark.parametrize(
    "paths", ["tests/missing.py", "tests/missing.py::test_case", "tests/existing.py tests/missing.py"]
)
def test_a_focused_proof_refuses_missing_paths_before_setup(paths, tmp_path):
    steps = _workflow()["jobs"]["tests"]["steps"]
    index = next(i for i, step in enumerate(steps) if step.get("name") == "Require explicit test paths")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests/existing.py").touch()
    result = subprocess.run(
        ["bash", "-e", "-c", steps[index]["run"]],
        env={**os.environ, "TEST_PATHS": paths},
        cwd=tmp_path,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 1
    assert "does not exist" in result.stdout
    assert index < next(i for i, step in enumerate(steps) if step.get("uses", "").startswith("actions/setup-python@"))


@pytest.mark.parametrize("paths", ["tests/missing.py tests/other.py", "tests/existing.py::test_case tests/missing.py"])
def test_focused_proof_names_every_missing_path_before_setup(paths, tmp_path):
    steps = _workflow()["jobs"]["tests"]["steps"]
    index = next(i for i, step in enumerate(steps) if step.get("name") == "Require explicit test paths")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests/existing.py").touch()
    result = subprocess.run(
        ["bash", "-e", "-c", steps[index]["run"]],
        env={**os.environ, "TEST_PATHS": paths},
        cwd=tmp_path,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 1
    for path in paths.split():
        if "existing" not in path:
            assert f"Test path {path} does not exist." in result.stdout
    assert index < next(i for i, step in enumerate(steps) if step.get("uses", "").startswith("actions/setup-python@"))
    assert index < next(i for i, step in enumerate(steps) if step.get("name") == "Install dependencies")


@pytest.mark.parametrize(("paths", "code"), [("tests/existing.py", 0), ("tests/missing.py", 1), ("", 1)])
def test_selected_unit_paths_are_checked_before_setup(paths, code, tmp_path):
    jobs = yaml.safe_load((_ROOT / ".github/workflows/test.yml").read_text())["jobs"]
    steps = jobs["unit-selected"]["steps"]
    index = next(i for i, step in enumerate(steps) if step.get("name") == "Require selected test paths")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests/existing.py").touch()
    result = subprocess.run(
        ["bash", "-e", "-c", steps[index]["run"]],
        env={**os.environ, "FILES": paths, "LEG": "1"},
        cwd=tmp_path,
        capture_output=True,
    )
    assert result.returncode == code, result.stdout + result.stderr
    assert index < next(i for i, step in enumerate(steps) if step.get("uses", "").startswith("actions/setup-python@"))


@pytest.mark.parametrize(
    ("paths", "existing_only", "code", "selected"),
    [
        ("tests/existing.py::test_case", "false", 0, None),
        ("tests/missing.py tests/existing.py::test_case tests/other.py", "true", 0, "tests/existing.py::test_case"),
        ("tests/missing.py tests/other.py", "true", 1, None),
        ("tests/existing.py tests/missing.py", "false", 1, None),
        ("tests/existing.py hooks/missing.py", "true", 1, None),
    ],
)
def test_focused_proof_runs_only_existing_requests_when_explicitly_asked(
    paths, existing_only, code, selected, tmp_path
):
    step = next(s for s in _workflow()["jobs"]["tests"]["steps"] if s.get("name") == "Require explicit test paths")
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests/existing.py").touch()
    output = tmp_path / "github-env"
    result = subprocess.run(
        ["bash", "-e", "-c", step["run"]],
        env={**os.environ, "TEST_PATHS": paths, "EXISTING_ONLY": existing_only, "GITHUB_ENV": str(output)},
        cwd=tmp_path,
        capture_output=True,
        text=True,
    )
    assert result.returncode == code, result.stdout + result.stderr
    if selected is None:
        assert not output.exists()
    else:
        assert output.read_text() == f"TEST_PATHS={selected}\n"
        assert "::warning::Test path tests/missing.py does not exist." in result.stdout
        assert "::warning::Test path tests/other.py does not exist." in result.stdout


def test_selected_queue_proof_names_every_missing_path_before_setup(tmp_path):
    jobs = yaml.safe_load((_ROOT / ".github/workflows/test.yml").read_text())["jobs"]
    step = next(s for s in jobs["unit-selected"]["steps"] if s.get("name") == "Require selected test paths")
    result = subprocess.run(
        ["bash", "-e", "-c", step["run"]],
        env={**os.environ, "FILES": "tests/missing.py tests/other.py", "LEG": "1"},
        cwd=tmp_path,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 1
    assert "Selected test path tests/missing.py does not exist." in result.stdout
    assert "Selected test path tests/other.py does not exist." in result.stdout


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
