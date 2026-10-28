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
    assert "postgres" not in scenario["services"]
    image = "public.ecr.aws/docker/library/postgres:16"
    steps = scenario["steps"]
    start = next(i for i, step in enumerate(steps) if step.get("name") == "Start Postgres from a retried pull")
    assert "if" not in steps[start]
    assert any(step.get("uses", "").startswith("actions/checkout@") for step in steps[:start])
    run = steps[start]["run"]
    pull = run.index(f"bash scripts/packaging/pull-runtime-image.sh {image}\n")
    assert pull < run.index(
        "docker run --pull never --rm -d --name writer-lock-postgres -p 127.0.0.1:5432:5432"
        f" -e POSTGRES_HOST_AUTH_METHOD=trust -e POSTGRES_DB=ledger {image}\n"
    )
    assert "until docker exec writer-lock-postgres pg_isready -h 127.0.0.1 -p 5432 -d ledger" in run
    assert scenario["timeout-minutes"] >= 15
    assert scenario["services"]["pgvector"]["image"] == "pgvector/pgvector:0.8.0-pg16"
    assert scenario["services"]["pgvector"]["ports"] == ["5433:5432"]
    assert scenario["env"]["MEMORY_TEST_POSTGRES_URL"] == "postgresql://postgres@127.0.0.1:5433/memory"
    assert "tests/swarm_ledger/test_postgres_writer_lock.py" in steps[-2]["run"]
    assert "tests/swarm_ledger/test_postgres_outbox.py" in steps[-2]["run"]
    assert "tests/swarm_ledger/test_postgres_writer_floor.py" in steps[-2]["run"]
    assert "tests/swarm_ledger/test_postgres_repository.py" in steps[-2]["run"]
    assert "tests/test_memory_postgres_parity.py" in steps[-2]["run"]
    assert "grep -q ' 16 passed'" in steps[-2]["run"]
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
        "tests/test_memory_postgres_parity.py",
        "tests/sv2_idx02_cases.py",
        "tests/sv2_idx04_cases.py",
        "tests/sv2_idx05_cases.py",
        "tests/fixtures/swarm_v2",
        "pyproject.toml",
        ".github/test-excludes.txt",
        ".github/workflows/writer-lock.yml",
    } <= set(entry["paths"])
    assert "postgres 16" in entry["tools"]
    assert "pgvector 0.8.0 pg16" in entry["tools"]


def test_writer_lock_install_uses_trusted_version_without_scanning_git():
    workflow = yaml.safe_load((ROOT / ".github/workflows/writer-lock.yml").read_text())
    steps = workflow["jobs"]["scenario"]["steps"]
    [install] = [step for step in steps if step.get("name") == "Install dependencies"]
    assert install.get("env", {}).get("SETUPTOOLS_SCM_PRETEND_VERSION_FOR_AGENTIHOOKS") == (
        "0.dev${{ github.run_number }}"
    )


def test_writer_lock_version_inference_bypasses_a_stalled_git_status(monkeypatch):
    import subprocess

    from setuptools_scm import get_version

    workflow = yaml.safe_load((ROOT / ".github/workflows/writer-lock.yml").read_text())
    steps = workflow["jobs"]["scenario"]["steps"]
    [install] = [step for step in steps if step.get("name") == "Install dependencies"]
    monkeypatch.delenv("SETUPTOOLS_SCM_PRETEND_VERSION", raising=False)
    monkeypatch.delenv("SETUPTOOLS_SCM_PRETEND_VERSION_FOR_AGENTIHOOKS", raising=False)

    def stalled_status(root, config):
        raise subprocess.TimeoutExpired(["git", "status", "--porcelain", "--untracked-files=no"], 40)

    with pytest.raises(subprocess.TimeoutExpired, match="40 seconds"):
        get_version(root=str(ROOT), dist_name="agentihooks", parse=stalled_status)
    for key, value in install.get("env", {}).items():
        monkeypatch.setenv(key, value.replace("${{ github.run_number }}", "123"))
    assert get_version(root=str(ROOT), dist_name="agentihooks", parse=stalled_status) == "0.dev123"
