import os
import subprocess
from pathlib import Path

import pytest

from scripts import ci_wiring

pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[1]
MAINTENANCE_OFF = {"GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "maintenance.auto", "GIT_CONFIG_VALUE_0": "false"}
EXPORTED = "".join(f'echo "{key}={value}" >> "$GITHUB_ENV"\n' for key, value in MAINTENANCE_OFF.items())


def _touches_repository(step: dict) -> bool:
    if str(step.get("uses", "")).startswith("actions/checkout@"):
        return "path" not in (step.get("with") or {})
    return any(command in str(step.get("run", "")) for command in ("git fetch", "git worktree"))


def _worktree_jobs() -> list[tuple[str, str, dict]]:
    return [
        (file, job_id, job)
        for file, workflow in ci_wiring.load(ROOT).items()
        for job_id, job in (workflow.get("jobs") or {}).items()
        if any("git worktree add" in str(step.get("run", "")) for step in job.get("steps") or [])
    ]


def test_every_worktree_job_turns_off_maintenance_before_its_first_checkout():
    jobs = _worktree_jobs()
    assert {(file, job_id) for file, job_id, _ in jobs} >= {
        ("test.yml", "select"),
        ("test.yml", "test-count"),
        ("test.yml", "unit"),
        ("test.yml", "unit-rerun"),
        ("test.yml", "lint"),
        ("test.yml", "coverage-ratchet"),
        ("test.yml", "mutation-plan"),
        ("count-record.yml", "count-record"),
        ("ledger-load.yml", "load"),
    }
    missing = []
    for file, job_id, job in jobs:
        if MAINTENANCE_OFF.items() <= (job.get("env") or {}).items():
            continue
        steps = job["steps"]
        first = next(index for index, step in enumerate(steps) if _touches_repository(step))
        if not any(step.get("run") == EXPORTED and "if" not in step for step in steps[:first]):
            missing.append(f"{file}/{job_id}")
    assert missing == []


def test_select_keeps_its_protected_provenance_context():
    workflow = ci_wiring.load(ROOT)["test.yml"]
    select = workflow["jobs"]["select"]
    assert "env" not in select
    assert "env" not in workflow
    exported = [index for index, step in enumerate(select["steps"]) if step.get("run") == EXPORTED]
    assert exported == [5]
    assert select["steps"][6]["uses"] == "actions/checkout@v4"


def test_git_reads_maintenance_off_from_the_environment(tmp_path):
    environment = {**os.environ, "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1"}
    subprocess.run(["git", "init", "-q", str(tmp_path)], env=environment, check=True)
    read = ["git", "-C", str(tmp_path), "config", "--get", "maintenance.auto"]
    assert subprocess.run(read, env=environment, capture_output=True, text=True).returncode == 1
    value = subprocess.run(read, env={**environment, **MAINTENANCE_OFF}, capture_output=True, text=True, check=True)
    assert value.stdout == "false\n"
