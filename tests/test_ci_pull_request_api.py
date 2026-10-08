import os
import re
import subprocess
from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[1]
API_CALL = re.compile(r"\bgh\b(?!-)|api\.github\.com|GITHUB_API_URL|collect\.py")
TOKEN = re.compile(r"github\.token|secrets\.(github|gh)_\w*", re.IGNORECASE)


def _workflow(name: str) -> dict:
    return yaml.safe_load((ROOT / ".github/workflows" / name).read_text())


def _jobs() -> dict:
    return _workflow("test.yml")["jobs"]


def _all_jobs():
    for name, job in _jobs().items():
        called = job.get("uses", "")
        if called.startswith("./.github/workflows/"):
            for inner, inner_job in _workflow(Path(called).name)["jobs"].items():
                yield f"{name}/{inner}", inner_job
        else:
            yield name, job


def _all_steps():
    for name, job in _all_jobs():
        if TOKEN.search(str(job.get("env", {}))):
            yield name, {"name": "job env", "env": job["env"]}
        for step in job.get("steps", []):
            yield name, step


def _holds_token(step: dict) -> bool:
    return bool(
        TOKEN.search(str(step.get("env", {})))
        or TOKEN.search(str(step.get("with", {})))
        or TOKEN.search(step.get("run", ""))
        or step.get("uses", "").startswith("actions/github-script@")
    )


def test_no_step_on_any_event_holds_the_workflow_token_or_calls_the_api():
    offenders = [
        f"{job}: {step.get('name') or step.get('uses')}"
        for job, step in _all_steps()
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
        {"run": "echo ${{ github.token }}"},
        {"uses": "actions/github-script@v7"},
        {"env": {"TOKEN": "${{ secrets.GH_PAT }}"}},
        {"run": 'gh api "repos/$GITHUB_REPOSITORY/actions/artifacts"'},
        {"run": "gh run download 42 --dir out"},
        {"run": "curl https://api.github.com/rate_limit"},
        {"uses": "actions/download-artifact@v4", "with": {"github-token": "${{ secrets.GITHUB_TOKEN }}"}},
    ],
    ids=[
        "gh-with-flags",
        "api-url",
        "github-token-env",
        "action-input",
        "token-in-run",
        "script-default",
        "pat",
        "gh-api",
        "gh-run-download",
        "api-host",
        "cross-run-download",
    ],
)
def test_each_way_of_reaching_the_api_is_an_offender(plant):
    assert _holds_token(plant) or API_CALL.search(plant.get("run", ""))


def test_sonar_downloads_this_runs_coverage_after_the_shards():
    sonar = _jobs()["sonar"]
    assert sonar["needs"] == ["unit"]
    steps = sonar["steps"]
    download = next(step for step in steps if step.get("name") == "Download shard coverage")
    merge = next(step for step in steps if step.get("name") == "Merge shard coverage")
    assert download["if"] == merge["if"] == "steps.current.outputs.superseded != 'true'"
    assert download["uses"].startswith("actions/download-artifact@")
    assert download["with"] == {"pattern": "coverage-3.12-*", "path": ".coverage-shards"}
    assert steps.index(download) < steps.index(merge)
    assert "env" not in merge
    assert merge["run"] == "bash .github/coverage/combine.sh --downloaded 8"


@pytest.mark.parametrize("job", ["unit", "shard-check", "test-count", "size", "lint"])
def test_a_dev_push_runs_every_step_of_the_job_itself(job):
    steps = _jobs()[job]["steps"]
    assert steps[0]["uses"] == "actions/checkout@v4"
    assert "if" not in steps[0]
    assert [step.get("name") for step in steps if "skip" in step.get("if", "")] == []


def test_no_job_is_granted_the_actions_api():
    assert [name for name, job in _jobs().items() if "actions" in job.get("permissions", {})] == []


def test_no_job_skips_on_a_tree_another_run_passed():
    jobs = _jobs()
    assert "record-pass" not in jobs
    assert "outputs" not in jobs["lint"]
    assert "steps.lookup" not in (ROOT / ".github/workflows/test.yml").read_text()


def _superseded(tmp_path, dev_head: str, sha: str = "a" * 40) -> str | None:
    step = next(step for step in _jobs()["sonar"]["steps"] if step.get("id") == "current")
    tools = tmp_path / "bin"
    tools.mkdir()
    listing = f'echo "{dev_head}\trefs/heads/dev"' if dev_head else "true"
    (tools / "git").write_text(f'#!/usr/bin/env bash\n[[ "$*" == "ls-remote origin refs/heads/dev" ]]\n{listing}\n')
    (tools / "git").chmod(0o755)
    output = tmp_path / "output"
    result = subprocess.run(
        ["bash", "-e", "-c", step["run"]],
        env={**os.environ, "PATH": f"{tools}:{os.environ['PATH']}", "GITHUB_OUTPUT": str(output), "SHA": sha},
        check=False,
    )
    return output.read_text() if result.returncode == 0 else None


@pytest.mark.parametrize(("dev_head", "superseded"), [("a" * 40, "false"), ("b" * 40, "true")])
def test_a_dev_push_skips_the_analysis_once_dev_moved_past_it(tmp_path, dev_head, superseded):
    assert _superseded(tmp_path, dev_head) == f"superseded={superseded}\n"


def test_an_unreadable_dev_head_fails_the_check_instead_of_skipping_the_analysis(tmp_path):
    assert _superseded(tmp_path, "") is None


def test_sonar_names_the_current_dev_head_from_git_and_gates_the_scan_on_it():
    steps = _jobs()["sonar"]["steps"]
    current = next(step for step in steps if step.get("id") == "current")
    assert current["if"] == "github.event_name == 'push'"
    assert current["env"] == {"SHA": "${{ github.sha }}"}
    names = [step.get("name") for step in steps]
    assert "Wait for older dev analyses" not in names
    later = steps[steps.index(current) + 1 :]
    gated = {"Download shard coverage", "Merge shard coverage", "SonarQube Scan", "SonarQube Quality Gate"}
    assert {
        step["name"] for step in later if "steps.current.outputs.superseded != 'true'" in step.get("if", "")
    } >= gated
