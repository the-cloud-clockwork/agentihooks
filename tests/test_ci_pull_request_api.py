import os
import re
import subprocess
from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[1]
PUSH_ONLY = "github.event_name == 'push'"
PUSH_TOKEN = "${{ github.event_name == 'push' && github.token || '' }}"
API_CALL = re.compile(r"\bgh\s+(api|run|pr|release|issue)\b|api\.github\.com|collect\.py")


def _jobs() -> dict:
    return yaml.safe_load((ROOT / ".github/workflows/test.yml").read_text())["jobs"]


def _pull_request_steps():
    for name, job in _jobs().items():
        if "steps" not in job or job.get("if") == PUSH_ONLY:
            continue
        for step in job["steps"]:
            if step.get("if") != PUSH_ONLY:
                yield name, step


def test_no_pull_request_step_holds_the_workflow_token_or_calls_the_api():
    offenders = [
        f"{job}: {step.get('name') or step.get('uses')}"
        for job, step in _pull_request_steps()
        if step.get("env", {}).get("GH_TOKEN", PUSH_TOKEN) != PUSH_TOKEN
        or "github.token" in str(step.get("with", {}))
        or API_CALL.search(step.get("run", ""))
    ]
    assert offenders == []


def test_sonar_downloads_this_runs_coverage_after_the_shards():
    sonar = _jobs()["sonar"]
    assert sonar["needs"] == ["unit"]
    steps = sonar["steps"]
    download = next(step for step in steps if step.get("name") == "Download shard coverage")
    merge = next(step for step in steps if step.get("name") == "Merge shard coverage")
    assert download["if"] == "github.event_name != 'push'"
    assert download["uses"].startswith("actions/download-artifact@")
    assert download["with"] == {"pattern": "coverage-3.12-*", "path": ".coverage-shards"}
    assert steps.index(download) < steps.index(merge)
    assert merge["env"]["SOURCE"] == "${{ github.event_name == 'push' && github.run_id || '--downloaded' }}"
    assert 'combine.sh "$SOURCE" 8' in merge["run"]


def test_record_pass_names_the_tree_lint_checked_out():
    jobs = _jobs()
    lint = jobs["lint"]
    assert lint["outputs"]["tree"] == "${{ steps.tree.outputs.sha }}"
    tree = next(step for step in lint["steps"] if step.get("id") == "tree")
    names = [step.get("uses") for step in lint["steps"]]
    assert names.index("actions/checkout@v4") < lint["steps"].index(tree)
    record, upload = jobs["record-pass"]["steps"]
    assert record["env"]["TREE"] == "${{ needs.lint.outputs.tree }}"
    assert upload["with"]["name"] == "tests-passed-${{ needs.lint.outputs.tree }}"


def test_lint_tree_step_writes_the_checked_out_tree(tmp_path):
    step = next(step for step in _jobs()["lint"]["steps"] if step.get("id") == "tree")
    repo = tmp_path / "repo"
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    (repo / "a").write_text("a")
    subprocess.run(["git", "-C", str(repo), "add", "a"], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "a"], check=True
    )
    tree = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD^{tree}"], capture_output=True, text=True, check=True
    ).stdout.strip()
    output = tmp_path / "output"
    subprocess.run(
        ["bash", "-e", "-c", step["run"]], cwd=repo, env={**os.environ, "GITHUB_OUTPUT": str(output)}, check=True
    )
    assert output.read_text() == f"sha={tree}\n"
