from pathlib import Path

import pytest
import yaml

WORKFLOWS = Path(__file__).resolve().parents[1] / ".github/workflows"
pytestmark = pytest.mark.unit


def test_the_ledger_load_gate_runs_beside_the_shards_with_redis_inside_the_budget():
    jobs = yaml.safe_load((WORKFLOWS / "test.yml").read_text())["jobs"]
    assert "ledger-load" in jobs["gate-required"]["needs"]
    job = jobs["ledger-load"]
    assert {key: value for key, value in job.items() if key not in {"needs", "if"}} == {
        "uses": "./.github/workflows/ledger-load.yml"
    }
    assert job.get("needs") in (None, ["reuse"])
    assert job.get("if") in (
        None,
        "${{ !cancelled() && (github.event_name != 'merge_group' || needs.reuse.outputs.reused != 'true') }}",
    )
    load = yaml.safe_load((WORKFLOWS / "ledger-load.yml").read_text())["jobs"]["load"]
    assert "redis" in load["services"]
    assert load["timeout-minutes"] <= 15
    assert "-m tests.ledger_load.gate --folder" in load["steps"][-1]["run"]
