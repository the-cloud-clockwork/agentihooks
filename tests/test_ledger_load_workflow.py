from pathlib import Path

import pytest
import yaml

WORKFLOWS = Path(__file__).resolve().parents[1] / ".github/workflows"
pytestmark = pytest.mark.unit
BASE = (
    "${{ github.event.pull_request.base.sha || github.event.merge_group.base_sha"
    " || github.event.before || inputs.base }}"
)


def test_the_ledger_load_gate_runs_beside_the_shards_with_redis_inside_the_budget():
    jobs = yaml.safe_load((WORKFLOWS / "test.yml").read_text())["jobs"]
    assert "ledger-load" in jobs["gate-required"]["needs"]
    job = jobs["ledger-load"]
    assert {key: value for key, value in job.items() if key not in {"needs", "if"}} == {
        "uses": "./.github/workflows/ledger-load.yml",
        "with": {"base": BASE},
    }
    assert job["needs"] in (["reuse"], ["reuse", "admission"])
    assert job["if"] in (
        "${{ !cancelled() && (github.event_name != 'merge_group' || needs.reuse.outputs.reused != 'true') }}",
        "${{ !cancelled() && (github.event_name != 'workflow_dispatch' || needs.admission.result == 'success') && (github.event_name != 'merge_group' || needs.reuse.outputs.reused != 'true') }}",
        "${{ !cancelled() && (github.event_name == 'push' || github.event_name == 'workflow_dispatch') && (github.event_name != 'workflow_dispatch' || needs.admission.result == 'success') }}",
    )
    load = yaml.safe_load((WORKFLOWS / "ledger-load.yml").read_text())["jobs"]["load"]
    assert any(step.get("name") == "Start Redis from a retried pull" for step in load["steps"])
    assert load["timeout-minutes"] <= 15
    gate = next(
        step
        for step in load["steps"]
        if step.get("name") == "Write and read a full size ledger from ten clients within the server budget"
    )
    assert "-m tests.ledger_load.gate --folder" in gate["run"]
    assert gate["if"] == "${{ !inputs.streams && !inputs.compare && steps.key.outputs.hit != 'true' }}"


def test_every_ledger_load_job_starts_redis_from_a_retried_pull():
    jobs = yaml.safe_load((WORKFLOWS / "ledger-load.yml").read_text())["jobs"]
    assert all("services" not in job for job in jobs.values())
    image = "public.ecr.aws/docker/library/redis:7-alpine"
    for name in ("load", "compare"):
        assert "'anton-oss'" in jobs[name]["runs-on"]
        steps = jobs[name]["steps"]
        start = next(i for i, step in enumerate(steps) if step.get("name") == "Start Redis from a retried pull")
        assert "if" not in steps[start]
        assert any(step.get("uses", "").startswith("actions/checkout@") for step in steps[:start])
        run = steps[start]["run"]
        pull = run.index(f"bash scripts/packaging/pull-runtime-image.sh {image}\n")
        assert pull < run.index(f"docker run --pull never --rm -d --name ledger-load-redis -p 6379:6379 {image}\n")
        assert "until docker exec ledger-load-redis redis-cli ping" in run


def test_the_ledger_load_gate_grades_with_the_budgets_of_the_fetched_base():
    workflow = yaml.safe_load((WORKFLOWS / "ledger-load.yml").read_text())
    triggers = workflow[True]
    assert triggers["workflow_call"]["inputs"]["base"]["required"] is True
    assert triggers["workflow_dispatch"]["inputs"]["base"]["required"] is True
    steps = workflow["jobs"]["load"]["steps"]
    fetch = next(step for step in steps if step.get("name") == "Fetch the base whose budgets grade the run")
    gate = next(
        step
        for step in steps
        if step.get("name") == "Write and read a full size ledger from ten clients within the server budget"
    )
    assert fetch["env"] == {"BASE": "${{ inputs.base }}"}
    assert fetch["run"] == 'git fetch --no-tags --depth=1 origin "${BASE#origin/}"'
    assert gate["run"].rstrip().endswith("--base FETCH_HEAD")
