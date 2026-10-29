import re
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
pytestmark = pytest.mark.unit

TEE_PIPE = re.compile(r"\|\s*tee\b")
SET_PIPEFAIL = re.compile(r"^\s*set\s+-[a-z]*o\s+pipefail\b|^\s*set\s+-o\s+pipefail\b", re.MULTILINE)


def _shell(workflow, job, step):
    for scope in (
        step,
        (job.get("defaults") or {}).get("run") or {},
        (workflow.get("defaults") or {}).get("run") or {},
    ):
        if "shell" in scope:
            return scope["shell"]
    return None


def _unguarded_tee_steps(workflow):
    unguarded = []
    for job_id, job in (workflow.get("jobs") or {}).items():
        for step in job.get("steps") or []:
            run = step.get("run")
            if not run or not TEE_PIPE.search(run):
                continue
            if _shell(workflow, job, step) == "bash" or SET_PIPEFAIL.search(run):
                continue
            unguarded.append(f"{job_id}: {step.get('name', run.splitlines()[0])}")
    return unguarded


def test_tests_workflow_tee_pipes_fail_when_their_command_fails():
    workflow = yaml.safe_load((ROOT / ".github/workflows/test.yml").read_text())
    assert _unguarded_tee_steps(workflow) == []


@pytest.mark.parametrize(
    ("step", "unguarded"),
    [
        ({"name": "s", "run": "cmd | tee out"}, True),
        ({"name": "s", "run": "cmd |tee -a out"}, True),
        ({"name": "s", "shell": "bash", "run": "cmd | tee out"}, False),
        ({"name": "s", "shell": "sh", "run": "cmd | tee out"}, True),
        ({"name": "s", "run": "set -o pipefail\ncmd | tee out"}, False),
        ({"name": "s", "run": "set -euo pipefail\ncmd | tee out"}, False),
        ({"name": "s", "run": "echo set -o pipefail | tee out"}, True),
        ({"name": "s", "run": "cmd > out"}, False),
        ({"name": "s", "run": "cmd | teeth"}, False),
    ],
)
def test_grader_refuses_a_tee_pipe_without_pipefail(step, unguarded):
    workflow = {"jobs": {"j": {"steps": [step]}}}
    assert bool(_unguarded_tee_steps(workflow)) is unguarded


def test_grader_accepts_bash_from_job_or_workflow_defaults():
    step = {"name": "s", "run": "cmd | tee out"}
    assert _unguarded_tee_steps({"jobs": {"j": {"defaults": {"run": {"shell": "bash"}}, "steps": [step]}}}) == []
    assert _unguarded_tee_steps({"defaults": {"run": {"shell": "bash"}}, "jobs": {"j": {"steps": [step]}}}) == []
