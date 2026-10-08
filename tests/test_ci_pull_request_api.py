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
API_CALL = re.compile(r"\bgh\b(?!-)|api\.github\.com|GITHUB_API_URL|collect\.py")
TOKEN = re.compile(r"github\.token|secrets\.github_token", re.IGNORECASE)


def _workflow(name: str) -> dict:
    return yaml.safe_load((ROOT / ".github/workflows" / name).read_text())


def _jobs() -> dict:
    return _workflow("test.yml")["jobs"]


def _pull_request_jobs():
    for name, job in _jobs().items():
        if job.get("if") == PUSH_ONLY:
            continue
        called = job.get("uses", "")
        if called.startswith("./.github/workflows/"):
            for inner, inner_job in _workflow(Path(called).name)["jobs"].items():
                yield f"{name}/{inner}", inner_job
        else:
            yield name, job


def _pull_request_steps():
    for name, job in _pull_request_jobs():
        if TOKEN.search(str(job.get("env", {}))):
            yield name, {"name": "job env", "env": job["env"]}
        for step in job.get("steps", []):
            if step.get("if") != PUSH_ONLY:
                yield name, step


def _holds_token(step: dict) -> bool:
    env = {key: value for key, value in step.get("env", {}).items() if value != PUSH_TOKEN}
    return bool(TOKEN.search(str(env)) or TOKEN.search(str(step.get("with", {}))))


def test_no_pull_request_step_holds_the_workflow_token_or_calls_the_api():
    offenders = [
        f"{job}: {step.get('name') or step.get('uses')}"
        for job, step in _pull_request_steps()
        if _holds_token(step) or API_CALL.search(step.get("run", ""))
    ]
    assert offenders == []


@pytest.mark.parametrize(
    "plant",
    [
        {"run": "gh --repo o/r api rate_limit"},
        {"run": 'curl "$GITHUB_API_URL/rate_limit"'},
        {"env": {"GITHUB_TOKEN": "${{ secrets.GITHUB_TOKEN }}"}},
        {"uses": "actions/github-script@v7", "with": {"github-token": "${{ github.token }}"}},
    ],
    ids=["gh-with-flags", "api-url", "github-token-env", "action-input"],
)
def test_each_way_of_reaching_the_api_is_an_offender(plant):
    assert _holds_token(plant) or API_CALL.search(plant.get("run", ""))


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
