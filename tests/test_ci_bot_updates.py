import json
import os
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest
import yaml

_ROOT = Path(__file__).resolve().parents[1]


def _workflow(name):
    return yaml.safe_load((_ROOT / ".github/workflows" / name).read_text())


def test_ci_creates_no_commits_or_bot_pull_requests():
    folder = _ROOT / ".github/workflows"
    for path in sorted([*folder.glob("*.yml"), *folder.glob("*.yaml")]):
        workflow = _workflow(path.name)
        for job in workflow["jobs"].values():
            for step in job.get("steps", []):
                command = step.get("run", "")
                assert "git commit" not in command
                assert "HEAD:dev" not in command
                assert "ci_bot_pr" not in command
        assert "ORG_GITHUB_TOKEN" not in json.dumps(workflow)
    assert not (_ROOT / ".github/workflows/refresh-durations.yml").exists()
    assert not (_ROOT / "scripts/ci_bot_pr.sh").exists()


def test_dev_push_publishes_merged_durations_with_read_permissions():
    job = _workflow("test.yml")["jobs"]["refresh-durations"]
    assert job["needs"] == ["unit", "lint"]
    assert job["if"] == "github.event_name == 'push'"
    assert job["permissions"] == {"contents": "read", "actions": "read"}
    upload = next(s for s in job["steps"] if s.get("uses") == "actions/upload-artifact@v4")
    assert upload["with"]["name"] == "durations-merged"
    assert upload["with"]["path"] == ".test_durations"
    assert upload["with"]["include-hidden-files"] is True


def test_a_newer_dev_push_never_cancels_a_running_dev_push_run():
    concurrency = _workflow("test.yml")["concurrency"]
    assert (concurrency["group"], concurrency["cancel-in-progress"]) in {
        ("tests-${{ github.ref }}", "${{ github.event_name != 'push' }}"),
        (
            "tests-${{ github.event_name }}-${{ github.event.pull_request.number || github.run_id }}",
            "${{ github.event_name == 'pull_request' }}",
        ),
    }


@pytest.mark.parametrize("mode", ["download", "no_run", "missing", "invalid"])
def test_pr_shards_use_the_newest_dev_durations_artifact_or_the_committed_fallback(tmp_path, mode):
    steps = _workflow("test.yml")["jobs"]["unit"]["steps"]
    step = next(s for s in steps if s.get("name") == "Download latest dev durations")
    assert step["env"]["GH_TOKEN"] == "${{ github.token }}"
    assert _workflow("test.yml")["jobs"]["unit"]["permissions"]["actions"] == "read"
    assert steps.index(step) < next(i for i, s in enumerate(steps) if s.get("name") == "Run tests")
    tools = tmp_path / "bin"
    tools.mkdir()
    gh = tools / "gh"
    gh.write_text(
        f"#!{sys.executable}\n"
        + """
import json
import os
import sys
from pathlib import Path
args = sys.argv[1:]
with Path("calls").open("a") as f:
    f.write(json.dumps(args) + "\\n")
if args[0] == "api":
    print("" if os.environ["MODE"] == "no_run" else "42")
elif os.environ["MODE"] == "missing":
    sys.exit(1)
else:
    folder = Path(args[args.index("--dir") + 1])
    folder.mkdir(parents=True, exist_ok=True)
    value = {"tests/a.py::test_a": 90.0, "tests/b.py::test_b": 1.0}
    if os.environ["MODE"] == "invalid":
        value = {"bad": "seconds"}
    (folder / ".test_durations").write_text(json.dumps(value))
"""
    )
    gh.chmod(0o755)
    committed = '{"committed": 0.1}'
    (tmp_path / ".test_durations").write_text(committed)
    result = subprocess.run(
        ["bash", "-euo", "pipefail", "-c", step["run"]],
        cwd=tmp_path,
        env={
            **os.environ,
            "PATH": f"{tools}:{os.environ['PATH']}",
            "MODE": mode,
            "GITHUB_REPOSITORY": "owner/repo",
            "RUNNER_TEMP": str(tmp_path),
        },
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    calls = [json.loads(line) for line in (tmp_path / "calls").read_text().splitlines()]
    assert "actions/artifacts?name=durations-merged" in calls[0][1]
    assert 'head_branch == "dev"' in calls[0][-1]
    if mode == "download":
        assert json.loads((tmp_path / ".test_durations").read_text()) == {
            "tests/a.py::test_a": 90.0,
            "tests/b.py::test_b": 1.0,
        }
        assert "42" in result.stdout
        assert "--name" in calls[1] and "durations-merged" in calls[1]
    else:
        assert (tmp_path / ".test_durations").read_text() == committed


@pytest.mark.parametrize("bump,expected", [("patch", "2.17.1"), ("minor", "2.18.0"), ("major", "3.0.0")])
def test_release_computes_the_requested_version_from_tags(tmp_path, bump, expected):
    step = next(s for s in _workflow("release.yml")["jobs"]["release"]["steps"] if s.get("id") == "version")
    tools = tmp_path / "bin"
    tools.mkdir()
    git = tools / "git"
    git.write_text('#!/usr/bin/env bash\nset -euo pipefail\nprintf "v2.17.0\\n"\n')
    git.chmod(0o755)
    output = tmp_path / "output"
    result = subprocess.run(
        ["bash", "-euo", "pipefail", "-c", step["run"]],
        cwd=tmp_path,
        env={**os.environ, "PATH": f"{tools}:{os.environ['PATH']}", "BUMP": bump, "GITHUB_OUTPUT": str(output)},
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert f"next={expected}\n" in output.read_text()


def test_release_tags_without_committing_and_dry_run_never_pushes():
    job = _workflow("release.yml")["jobs"]["release"]
    assert job["permissions"] == {"contents": "write", "actions": "write"}
    assert job["env"]["GH_TOKEN"] == "${{ github.token }}"
    tag = next(s for s in job["steps"] if s.get("name") == "Tag the release")
    assert 'git tag "v$NEXT" "$GITHUB_SHA"' in tag["run"]
    assert "git rev-parse HEAD" in tag["run"]
    for step in job["steps"]:
        if "git push" in step.get("run", "") or "gh release create" in step.get("run", ""):
            assert step["if"] == "${{ !inputs.dry_run }}"
    assert any(s.get("name") == "Verify the tagged package version" for s in job["steps"])


def test_package_version_is_derived_from_git():
    project = tomllib.loads((_ROOT / "pyproject.toml").read_text())
    assert "version" not in project["project"]
    assert project["project"]["dynamic"] == ["version"]
    assert any(dep.startswith("setuptools-scm") for dep in project["build-system"]["requires"])
    assert "setuptools_scm" in project["tool"]
    checkout = next(
        s for s in _workflow("publish-pypi.yml")["jobs"]["publish"]["steps"] if s.get("uses") == "actions/checkout@v4"
    )
    assert checkout["with"]["fetch-depth"] == 0
