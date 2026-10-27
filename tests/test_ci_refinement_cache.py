from pathlib import Path

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
    assert "hashFiles('.mutation-stats-cache/**') != ''" in first["if"]
    upload = next(step for step in steps if step.get("name") == "Upload the measured mutation plan")
    assert steps.index(upload) < steps.index(first) < steps.index(retry)
    assert not upload.get("continue-on-error", False)
    refine = next(step for step in steps if step.get("id") == "refine")
    assert steps.index(refine) < steps.index(upload)
    assert not refine.get("continue-on-error", False)
