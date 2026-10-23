import json
from pathlib import Path

import pytest
import yaml

from scripts.ci_branch_lanes import problems

pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[1]


def test_writer_lock_keys_inputs_before_starting_the_postgres_scenario():
    caller = yaml.safe_load((ROOT / ".github/workflows/test.yml").read_text())["jobs"]["writer-lock"]
    assert caller["uses"] == "./.github/workflows/writer-lock.yml"
    workflow = yaml.safe_load((ROOT / ".github/workflows/writer-lock.yml").read_text())
    assert problems(workflow) == []
    jobs = workflow["jobs"]
    key = jobs["inputs"]
    assert "services" not in key
    assert key["outputs"] == {
        "hit": "${{ steps.key.outputs.hit }}",
        "key": "${{ steps.key.outputs.key }}",
    }
    [lookup] = [step for step in key["steps"] if step.get("uses") == "./.github/actions/input-key"]
    assert lookup["id"] == "key"
    assert lookup["with"]["job"] == "writer-lock"
    scenario = jobs["scenario"]
    assert scenario["needs"] == ["inputs"]
    assert scenario["if"] == "github.event_name == 'workflow_dispatch' || needs.inputs.outputs.hit != 'true'"
    assert scenario["services"]["postgres"]["image"] == "public.ecr.aws/docker/library/postgres:16"
    steps = scenario["steps"]
    assert "tests/swarm_ledger/test_postgres_writer_lock.py" in steps[-2]["run"]
    assert "tests/swarm_ledger/test_postgres_outbox.py" in steps[-2]["run"]
    assert "tests/swarm_ledger/test_postgres_writer_floor.py" in steps[-2]["run"]
    assert "grep -q ' 10 passed'" in steps[-2]["run"]
    assert steps[-1]["uses"] == "./.github/actions/input-key/save"
    assert steps[-1]["if"] == "github.event_name == 'push' || github.event_name == 'workflow_dispatch'"
    assert steps[-1]["with"] == {"key": "${{ needs.inputs.outputs.key }}"}


def test_writer_lock_key_covers_the_scenario_repository_and_install_inputs():
    entry = json.loads((ROOT / ".github/input-keys.json").read_text())["writer-lock"]
    assert {
        "scripts",
        "profiles",
        "hooks",
        "tests/conftest.py",
        "tests/swarm_ledger",
        "pyproject.toml",
        ".github/test-excludes.txt",
        ".github/workflows/writer-lock.yml",
    } <= set(entry["paths"])
    assert "postgres 16" in entry["tools"]
