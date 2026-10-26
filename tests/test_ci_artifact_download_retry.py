import os
import subprocess
from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[1]
# The protected grader compares test-summary with its reviewed form from the base branch.
# shard-check takes its retry through its own pull request.
EXEMPT = {"test-summary", "shard-check"}


def _jobs():
    return yaml.safe_load((ROOT / ".github/workflows/test.yml").read_text())["jobs"]


def _first_attempts():
    for job, spec in _jobs().items():
        steps = spec.get("steps") or []
        for index, step in enumerate(steps):
            if step.get("uses", "").startswith("actions/download-artifact@") and not step["name"].endswith(", attempt 2"):
                yield job, steps, index


def test_every_tests_job_downloading_artifacts_is_covered():
    jobs = {job for job, _, _ in _first_attempts()}
    assert {
        "select",
        "split",
        "unit",
        "unit-rerun",
        "unit-selected",
        "sonar",
        "coverage-ratchet",
        "refresh-durations",
        "flake-budget",
        "coverage-baseline",
        "mutation-stats",
        "mutation",
    } <= jobs - EXEMPT


@pytest.mark.parametrize(
    "job, index",
    [(job, index) for job, _, index in _first_attempts() if job not in EXEMPT],
)
def test_a_failed_download_names_the_outage_waits_and_downloads_once_more(job, index, tmp_path):
    steps = _jobs()[job]["steps"]
    first, wait, second = steps[index : index + 3]
    assert first["continue-on-error"] is True
    retry = f"steps.{first['id']}.outcome == 'failure'"
    assert wait["if"] == second["if"] == retry
    assert wait["name"] == f"{first['name']}, wait before attempt 2"
    assert second["name"] == f"{first['name']}, attempt 2"
    assert second["uses"] == first["uses"]
    assert second["with"] == first["with"]
    assert "continue-on-error" not in second
    assert [s.get("id") for s in steps].count(first["id"]) == 1
    binary = tmp_path / "bin"
    binary.mkdir()
    (binary / "sleep").write_text('#!/usr/bin/env bash\necho "$1" >> "$SLEPT"\n')
    (binary / "sleep").chmod(0o755)
    result = subprocess.run(
        [wait["shell"], "-e", "-c", wait["run"]],
        env=dict(os.environ, PATH=f"{binary}:{os.environ['PATH']}", SLEPT=str(tmp_path / "slept")),
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0
    assert result.stdout.startswith(f"::warning::{first['name']} failed, as an artifact service outage does;")
    assert (tmp_path / "slept").read_text() == "30\n"
