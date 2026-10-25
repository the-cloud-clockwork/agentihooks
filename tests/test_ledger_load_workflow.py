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
    assert "redis" in load["services"]
    assert load["timeout-minutes"] <= 15
    gate = next(
        step
        for step in load["steps"]
        if step.get("name") == "Write and read a full size ledger from ten clients within the server budget"
    )
    assert "-m tests.ledger_load.gate --folder" in gate["run"]
    assert gate["if"] == "${{ !inputs.streams && !inputs.compare && steps.key.outputs.hit != 'true' }}"


def test_every_ledger_load_service_pulls_redis_from_the_google_mirror():
    jobs = yaml.safe_load((WORKFLOWS / "ledger-load.yml").read_text())["jobs"]
    images = {name: job["services"]["redis"]["image"] for name, job in jobs.items() if "services" in job}
    assert images == {
        "load": "mirror.gcr.io/library/redis:7-alpine",
        "compare": "mirror.gcr.io/library/redis:7-alpine",
    }
    assert all(
        not service["image"].startswith("public.ecr.aws/")
        for job in jobs.values()
        for service in job.get("services", {}).values()
    )


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
