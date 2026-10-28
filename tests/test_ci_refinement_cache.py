import os
import subprocess
from pathlib import Path

import pytest
import yaml


def test_optional_refinement_cache_retries_fit_inside_the_job_budget():
    workflow = Path(__file__).resolve().parents[1] / ".github/workflows/mutation-preflight.yml"
    job = yaml.safe_load(workflow.read_text())["jobs"]["refine"]
    steps = job["steps"]
    saves = [step for step in steps if step.get("uses") == "actions/cache/save@v4"]
    assert len(saves) == 2
    first, retry = saves
    assert all(step["timeout-minutes"] == 1 and step["continue-on-error"] is True for step in saves)
    assert sum(step["timeout-minutes"] for step in saves) < job["timeout-minutes"]
    assert retry["with"] == first["with"]
    assert retry["if"] == f"!cancelled() && steps.{first['id']}.outcome == 'failure'"
    assert "success()" in first["if"]
    assert first["if"] == (
        "success() && steps.merged-stats.outputs.found == 'true' && steps.cache-budget.outputs.available == 'true'"
    )
    assert not any("hashFiles" in step.get("if", "") for step in saves)
    check = next(step for step in steps if step.get("id") == "merged-stats")
    assert check["timeout-minutes"] == 1
    assert check["continue-on-error"] is True
    budget = next(step for step in steps if step.get("id") == "cache-budget")
    assert budget["continue-on-error"] is True
    preparation = next(step for step in steps if step.get("id") == "cache-preparation-budget")
    assert preparation["continue-on-error"] is True
    assert check["if"] == "steps.cache-preparation-budget.outputs.available == 'true'"
    assert steps.index(preparation) < steps.index(check) < steps.index(budget) < steps.index(first)
    assert steps.index(check) < steps.index(first)
    upload = next(step for step in steps if step.get("name") == "Upload the measured mutation plan")
    assert steps.index(upload) < steps.index(first) < steps.index(retry)
    assert not upload.get("continue-on-error", False)
    refine = next(step for step in steps if step.get("id") == "refine")
    assert steps.index(refine) < steps.index(upload)
    assert not refine.get("continue-on-error", False)


@pytest.mark.parametrize(("budget_id", "limit"), [("cache-budget", 120), ("cache-preparation-budget", 180)])
@pytest.mark.parametrize(("remaining", "available"), [(120, True), (1, True), (0, False), (-180, False)])
def test_refinement_skips_cache_without_time_for_both_attempts(tmp_path, budget_id, limit, remaining, available):
    workflow = Path(__file__).resolve().parents[1] / ".github/workflows/mutation-preflight.yml"
    job = yaml.safe_load(workflow.read_text())["jobs"]["refine"]
    start = next(step for step in job["steps"] if step.get("id") == "refinement-start")
    assert job["steps"][0] == start
    assert "started=$(date +%s)" in start["run"]
    budget = next(step for step in job["steps"] if step.get("id") == budget_id)
    elapsed = limit - remaining
    assert budget["env"] == {"REFINEMENT_STARTED_AT": "${{ steps.refinement-start.outputs.started }}"}
    clock = tmp_path / "date"
    clock.write_text("#!/usr/bin/env bash\necho 1000\n")
    clock.chmod(0o755)
    output = tmp_path / "output"
    subprocess.run(
        ["bash", "-e", "-c", budget["run"]],
        env={
            **os.environ,
            "PATH": f"{tmp_path}:{os.environ['PATH']}",
            "REFINEMENT_STARTED_AT": str(1000 - elapsed),
            "GITHUB_OUTPUT": str(output),
        },
        check=True,
        timeout=2,
    )
    assert (output.read_text() if output.exists() else "") == ("available=true\n" if available else "")
