import json
import os
import subprocess
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
    assert job["needs"] == ["unit", "lint", "shard-check"]
    assert job["if"] == "${{ !cancelled() && github.event_name == 'push' }}"
    assert job["permissions"] == {"contents": "read"}
    upload = next(s for s in job["steps"] if s.get("uses") == "actions/upload-artifact@v4")
    assert upload["with"]["name"] == "durations-merged"
    assert upload["with"]["path"] == ".test_durations*"
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


ADOPT = 'python -m tests.dev_durations "$PYTHON_VERSION" ~/dev-durations --hash durations.sha256'
ADOPT_ENV = {"PYTHON_VERSION": "${{ matrix.python-version }}"}


def test_unit_shards_adopt_dev_durations_through_the_script_before_the_tests_run():
    steps = _workflow("test.yml")["jobs"]["unit"]["steps"]
    step = next(s for s in steps if s.get("name") == "Adopt latest dev durations")
    assert step["run"].strip() == ADOPT
    assert step["env"] == ADOPT_ENV
    assert steps.index(step) < next(i for i, s in enumerate(steps) if s.get("name") == "Run tests")


def test_unit_shards_restore_dev_durations_from_the_cache_the_dev_push_saves():
    jobs = _workflow("test.yml")["jobs"]
    steps = jobs["unit"]["steps"]
    restore = next(s for s in steps if s.get("name") == "Restore latest dev durations")
    adopt = next(s for s in steps if s.get("name") == "Adopt latest dev durations")
    refresh = jobs["refresh-durations"]
    save = next(s for s in refresh["steps"] if s.get("uses") == "actions/cache/save@v4")
    stage = next(s for s in refresh["steps"] if s.get("name") == "Stage merged durations for the cache")
    assert refresh["if"] == "${{ !cancelled() && github.event_name == 'push' }}"
    assert refresh["steps"].index(stage) == refresh["steps"].index(save) - 1
    assert restore["uses"] == "actions/cache/restore@v4"
    assert steps.index(restore) == steps.index(adopt) - 1
    assert "restore-keys" not in restore["with"]
    assert restore["with"]["path"] == save["with"]["path"] == "~/dev-durations"
    assert save["with"]["key"] == "durations-merged-${{ github.sha }}"
    assert restore["with"]["key"] == "${{ needs.durations.outputs.key }}"
    assert restore["if"] == "needs.durations.outputs.key != ''"
    assert restore["with"]["fail-on-cache-miss"] is True
    assert jobs["unit"]["needs"] == ["durations"]
    lookup = jobs["durations"]["steps"][0]
    assert jobs["durations"]["outputs"] == {
        "key": "${{ steps.stored.outputs.cache-matched-key }}",
        "queued": "${{ steps.republished.outputs.queued }}",
    }
    assert jobs["durations"]["timeout-minutes"] == (
        "${{ (github.event_name == 'merge_group' || github.event_name == 'workflow_dispatch') && 4 || 2 }}"
    )
    assert lookup["id"] == "stored"
    assert lookup["uses"] == "actions/cache/restore@v4"
    assert lookup["with"] == {
        "path": "~/dev-durations",
        "key": "durations-merged-${{ github.event.pull_request.base.sha || github.event.merge_group.base_sha"
        " || github.event.before || github.sha }}",
        "lookup-only": True,
    }
    assert "durations" in jobs["gate-required"]["needs"]
    assert "run" not in restore
    assert adopt["run"] == ADOPT
    assert adopt["env"] == ADOPT_ENV


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


def test_publish_proves_the_published_version_installs_clean():
    workflow = _workflow("publish-pypi.yml")
    on = workflow.get("on", workflow.get(True))
    assert "version" in on["workflow_dispatch"]["inputs"]
    publish, verify = workflow["jobs"]["publish"], workflow["jobs"]["verify"]
    assert publish["if"] == "${{ !inputs.version }}"
    assert publish["outputs"]["version"]
    assert verify["needs"] == "publish"
    assert "environment" not in verify
    script = "\n".join(s.get("run", "") for s in verify["steps"])
    assert "https://pypi.org/pypi/agentihooks/$VERSION/json" in script
    assert '"agentihooks==$VERSION"' in script
    assert '"agentihooks $VERSION"' in script
