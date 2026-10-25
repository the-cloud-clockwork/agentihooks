import os
import subprocess
from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.unit

_ROOT = Path(__file__).resolve().parents[1]


def _sonar():
    return yaml.safe_load((_ROOT / ".github/workflows/test.yml").read_text())["jobs"]["sonar"]


def _step(name):
    return next(step for step in _sonar()["steps"] if step.get("name") == name)


@pytest.mark.parametrize(
    ("paths", "event", "expected"),
    [
        (["tests/test_one.py"], "pull_request", True),
        (["tests/nested/test_two.py", "tests/a name\nwith newline.txt"], "pull_request", True),
        (["hooks/one.py"], "pull_request", False),
        (["tests/test_one.py", "scripts/one.py"], "pull_request", False),
        (["sonar-project.properties"], "pull_request", False),
        ([".github/workflows/test.yml"], "pull_request", False),
        ([], "pull_request", False),
        (["tests/test_one.py"], "push", False),
        (["tests/test_one.py"], "workflow_dispatch", False),
    ],
)
def test_only_nonempty_test_only_pull_request_diffs_avoid_analysis(tmp_path, paths, event, expected):
    def git(*args):
        return subprocess.check_output(["git", *args], cwd=tmp_path, text=True).strip()

    git("init", "-q")
    git("-c", "user.name=proof", "-c", "user.email=proof@example.invalid", "commit", "--allow-empty", "-qm", "base")
    base = git("rev-parse", "HEAD")
    for name in paths:
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("changed\n")
        git("add", "--", name)
    git("-c", "user.name=proof", "-c", "user.email=proof@example.invalid", "commit", "--allow-empty", "-qm", "head")
    output = tmp_path / "output"
    result = subprocess.run(
        ["bash", "-e", "-o", "pipefail", "-c", _step("Decide whether Sonar has changed source")["run"]],
        cwd=tmp_path,
        env=dict(
            os.environ,
            BASE=base,
            HEAD=git("rev-parse", "HEAD"),
            EVENT=event,
            GITHUB_OUTPUT=str(output),
            GITHUB_ENV=str(tmp_path / "environment"),
            RUNNER_TEMP=str(tmp_path),
        ),
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert f"test_only={str(expected).lower()}" in output.read_text().splitlines()
    assert f"SONAR_TEST_ONLY={str(expected).lower()}" in (tmp_path / "environment").read_text().splitlines()


def test_the_decision_uses_immutable_event_revisions_and_retains_source_renames():
    step = _step("Decide whether Sonar has changed source")
    assert step["env"]["BASE"] == "${{ github.event.pull_request.base.sha }}"
    assert step["env"]["HEAD"] == "${{ github.sha }}"
    assert "--no-renames" in step["run"]


@pytest.mark.parametrize(
    ("before", "after", "expected"),
    [
        ("hooks/old.py", "tests/test_new.py", False),
        ("tests/test_old.py", "hooks/new.py", False),
        ("tests/test_old.py", "tests/test_new.py", True),
        ("hooks/old.py", None, False),
        ("tests/test_old.py", None, True),
    ],
)
def test_moves_and_deletions_keep_both_sides_of_the_source_boundary(tmp_path, before, after, expected):
    def git(*args):
        return subprocess.check_output(["git", *args], cwd=tmp_path, text=True).strip()

    git("init", "-q")
    old = tmp_path / before
    old.parent.mkdir(parents=True)
    old.write_text("unchanged contents\n")
    git("add", "--", before)
    git("-c", "user.name=proof", "-c", "user.email=proof@example.invalid", "commit", "-qm", "base")
    base = git("rev-parse", "HEAD")
    if after:
        (tmp_path / after).parent.mkdir(parents=True, exist_ok=True)
        git("mv", before, after)
    else:
        git("rm", before)
    git("-c", "user.name=proof", "-c", "user.email=proof@example.invalid", "commit", "-qm", "head")
    output = tmp_path / "output"
    result = subprocess.run(
        ["bash", "-e", "-o", "pipefail", "-c", _step("Decide whether Sonar has changed source")["run"]],
        cwd=tmp_path,
        env=dict(
            os.environ,
            BASE=base,
            HEAD=git("rev-parse", "HEAD"),
            EVENT="pull_request",
            GITHUB_OUTPUT=str(output),
            GITHUB_ENV=str(tmp_path / "environment"),
            RUNNER_TEMP=str(tmp_path),
        ),
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert f"test_only={str(expected).lower()}" in output.read_text().splitlines()
    assert f"SONAR_TEST_ONLY={str(expected).lower()}" in (tmp_path / "environment").read_text().splitlines()


def test_a_failed_diff_cannot_publish_a_test_only_decision(tmp_path):
    output = tmp_path / "output"
    result = subprocess.run(
        ["bash", "-e", "-o", "pipefail", "-c", _step("Decide whether Sonar has changed source")["run"]],
        cwd=tmp_path,
        env=dict(
            os.environ,
            BASE="missing",
            HEAD="missing",
            EVENT="pull_request",
            GITHUB_OUTPUT=str(output),
            GITHUB_ENV=str(tmp_path / "environment"),
            RUNNER_TEMP=str(tmp_path),
        ),
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert not output.exists() or "test_only=true" not in output.read_text()


@pytest.mark.parametrize(
    "name",
    [
        "Install coverage",
        "Build the test dependency manifest",
        "Start Cloudflare Access proxy",
        "Wait for the test dependency manifest",
        "Upload the test dependency manifest",
    ],
)
def test_test_only_changes_do_not_start_analysis_work(name):
    assert "steps.source.outputs.test_only != 'true'" in _step(name)["if"]


@pytest.mark.parametrize("name", ["Merge shard coverage", "SonarQube Scan", "SonarQube Quality Gate"])
def test_test_only_analysis_scripts_exit_without_tools_or_reports(tmp_path, name):
    result = subprocess.run(
        ["bash", "-e", "-c", _step(name)["run"]],
        cwd=tmp_path,
        env=dict(os.environ, SONAR_TEST_ONLY="true"),
        capture_output=True,
        text=True,
        timeout=5,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert list(tmp_path.iterdir()) == []


def test_test_only_coverage_wait_exits_without_a_merge(tmp_path):
    result = subprocess.run(
        ["bash", "-e", "-c", _step("Wait for the coverage merge")["run"]],
        env=dict(os.environ, TEST_ONLY="true", RUNNER_TEMP=str(tmp_path), MERGE_PID=""),
        capture_output=True,
        text=True,
        timeout=5,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_test_only_success_keeps_a_decision_in_the_analysis_evidence():
    assert "sonar-scope.txt" in _step("Upload coverage and analysis evidence")["with"]["path"].splitlines()
    assert "sonar-scope.txt" in _step("Decide whether Sonar has changed source")["run"]
    assert "sonar" in _sonar_gate_needs()


def _sonar_gate_needs():
    return yaml.safe_load((_ROOT / ".github/workflows/test.yml").read_text())["jobs"]["gate-required"]["needs"]
