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
    upload = next(s for s in job["steps"] if s.get("uses") == "./.github/actions/retry-upload")
    assert upload["with"]["name"] == "durations-merged"
    assert upload["with"]["path"] == ".test_durations*"
    assert upload["with"]["include-hidden-files"] is True


def test_a_newer_dev_push_replaces_only_the_pending_dev_push_run():
    concurrency = _workflow("test.yml")["concurrency"]
    assert concurrency["cancel-in-progress"] == "${{ github.event_name == 'pull_request' }}"
    assert concurrency["queue"] == "${{ github.event_name == 'workflow_dispatch' && 'max' || 'single' }}"
    assert "github.event_name == 'push' && 'promotion-dev'" in concurrency["group"]


ADOPT = (
    'python -m tests.dev_durations "$PYTHON_VERSION" ~/dev-durations --hash durations.sha256 --collected collected.json'
)
PRECOLLECTED_ADOPT = (
    'collection=()\nif [[ "$GITHUB_EVENT_NAME" != push ]]; then\n'
    "  collection=(--collection collected.json)\nfi\n"
    + ADOPT.replace(" --collected collected.json", ' "${collection[@]}" --collected collected.json')
)
ADOPT_ENV = {"PYTHON_VERSION": "${{ matrix.python-version }}"}


def test_unit_shards_adopt_dev_durations_through_the_script_before_the_tests_run():
    jobs = _workflow("test.yml")["jobs"]
    step = next(s for s in jobs["split"]["steps"] if s.get("name") == "Adopt latest dev durations")
    assert step["run"].strip() in (ADOPT, PRECOLLECTED_ADOPT)
    assert step["env"] == ADOPT_ENV
    steps = jobs["unit"]["steps"]
    download = next(s for s in steps if s.get("name") == "Download the durations this run splits on")
    assert download["with"] == {
        "name": "split-${{ matrix.python-version }}",
        "client-id": "${{ secrets.TCC_CI_CLIENT_ID }}",
        "private-key": "${{ secrets.TCC_CI_APP_PRIVATE_KEY }}",
    }
    assert steps.index(download) < next(i for i, s in enumerate(steps) if s.get("name") == "Run tests")
    assert jobs["unit"]["needs"] == ["split", "select", "reuse", "admission"]


def test_unit_shards_restore_dev_durations_from_the_cache_the_dev_push_saves():
    jobs = _workflow("test.yml")["jobs"]
    steps = jobs["split"]["steps"]
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
    producer = "durations" if "durations" in jobs else "select"
    assert restore["with"]["key"] == "${{ needs." + producer + ".outputs.key }}"
    assert restore["if"] == f"needs.{producer}.outputs.source == '' && needs.{producer}.outputs.key != ''"
    assert restore["with"]["fail-on-cache-miss"] is True
    assert jobs["split"]["needs"] in (
        ["durations", "select", "reuse", "admission"],
        ["select", "reuse", "admission"],
        ["test-count", "select", "reuse", "admission"],
    )
    lookup = next(s for s in jobs.get("durations", jobs["select"])["steps"] if s.get("id") == "stored")
    assert {
        key: jobs.get("durations", jobs["select"])["outputs"][key] for key in ("key", "queued", "base", "source")
    } == {
        "key": "${{ steps.stored.outputs.cache-matched-key }}",
        "queued": "${{ steps.republished.outputs.queued }}",
        "base": "${{ steps.base-run.outputs.sha }}",
        "source": "${{ steps.unit-source.outputs.source }}",
    }
    assert jobs.get("durations", jobs["select"])["timeout-minutes"] in (
        5,
        "${{ (github.event_name == 'merge_group' || github.event_name == 'workflow_dispatch') && 4 || 2 }}",
    )
    assert lookup["id"] == "stored"
    assert lookup["uses"] == "actions/cache/restore@v4"
    assert lookup["with"] == {
        "path": "~/dev-durations",
        "key": "durations-merged-${{ github.event.pull_request.base.sha || github.event.merge_group.base_sha"
        " || github.event.before || github.sha }}",
        "restore-keys": "durations-merged-",
        "lookup-only": True,
    }
    assert {"durations" if "durations" in jobs else "select", "split"} <= set(jobs["gate-required"]["needs"])
    assert "run" not in restore
    assert adopt["run"] in (ADOPT, PRECOLLECTED_ADOPT)
    assert adopt["env"] == ADOPT_ENV


@pytest.mark.parametrize("name", ["mutation-stats", "mutation"])
def test_mutation_jobs_adopt_dev_durations_before_the_sandbox_copies_the_record(name):
    steps = _workflow("test.yml")["jobs"][name]["steps"]
    names = [step.get("name") for step in steps]
    restore = steps[names.index("Restore latest dev durations")]
    adopt = steps[names.index("Adopt latest dev durations")]
    mutate = next(i for i, step in enumerate(steps) if "python -m scripts.ci_mutation" in step.get("run", ""))
    assert names.index("Restore latest dev durations") + 1 == names.index("Adopt latest dev durations") < mutate
    assert restore["uses"] == "actions/cache/restore@v4"
    assert restore["with"]["path"] == "~/dev-durations"
    assert restore["with"]["restore-keys"] == "durations-merged-"
    assert "from tests.dev_durations import adopt" in adopt["run"]
    assert 'Path(".test_durations").write_text' in adopt["run"]


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
        cwd=_ROOT,
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
    assert 'git tag "v$NEXT" "$GITHUB_SHA"' in tag["run"] or tag["run"] == (
        'bash scripts/ci_release.sh tag "$GITHUB_SHA" "$NEXT"'
    )
    script = (_ROOT / "scripts/ci_release.sh").read_text() if "ci_release.sh" in tag["run"] else tag["run"]
    assert "git rev-parse HEAD" in script
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
