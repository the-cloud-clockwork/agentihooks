import json
import os
import re
import subprocess
import tomllib
from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.unit

_ROOT = Path(__file__).parent.parent


def _pytest_command() -> str:
    for line in (_ROOT / ".github/workflows/test.yml").read_text().splitlines():
        if "pytest" in line and line.strip().startswith("run:"):
            return line
    raise AssertionError("no pytest step in test.yml")


@pytest.mark.parametrize("plugin", ["pytest-xdist", "pytest-split"])
def test_dev_extra_declares_test_plugin(plugin):
    dev = tomllib.loads((_ROOT / "pyproject.toml").read_text())["project"]["optional-dependencies"]["dev"]
    assert any(dep.startswith(plugin) for dep in dev)


def test_workflow_runs_whole_suite_in_parallel():
    command = _pytest_command()
    assert "-n auto" in command
    assert "tests/" in command
    assert "-m unit" not in command


def test_unit_shards_measure_no_coverage():
    command = _pytest_command()
    assert "--cov" not in command
    assert "matrix.cov" not in command
    assert "include" not in _workflow()["jobs"]["unit"]["strategy"]["matrix"]
    _, step = _unit_step_index(lambda s: s.get("name") == "Run tests")
    assert "COVERAGE_CORE" not in step.get("env", {})


def _setup_python_steps(job: str) -> list[dict]:
    workflow = yaml.safe_load((_ROOT / ".github/workflows/test.yml").read_text())
    return [s for s in workflow["jobs"][job]["steps"] if s.get("uses", "").startswith("actions/setup-python")]


def test_lint_setup_python_caches_pip_keyed_on_pyproject():
    (step,) = _setup_python_steps("lint")
    assert step["with"]["cache"] == "pip"
    assert step["with"]["cache-dependency-path"] == "pyproject.toml"


def test_unit_setup_python_restores_no_pip_cache():
    (step,) = _setup_python_steps("unit")
    assert "cache" not in step["with"]


def _unit_step_index(predicate) -> tuple[int, dict]:
    steps = yaml.safe_load((_ROOT / ".github/workflows/test.yml").read_text())["jobs"]["unit"]["steps"]
    (index,) = [i for i, s in enumerate(steps) if predicate(s)]
    return index, steps[index]


def test_unit_installs_extras_with_uv_cached_on_pyproject():
    uv_index, uv = _unit_step_index(lambda s: s.get("uses", "").startswith("astral-sh/setup-uv"))
    install_index, install = _unit_step_index(lambda s: s.get("name") == "Install dependencies")
    assert uv["with"]["enable-cache"] is True
    assert uv["with"]["cache-dependency-glob"] == "pyproject.toml"
    assert uv_index < install_index
    assert install["run"].strip() == 'uv pip install --system -e ".[dev,all]"'


def test_only_the_first_shard_saves_the_uv_cache():
    _, uv = _unit_step_index(lambda s: s.get("uses", "").startswith("astral-sh/setup-uv"))
    assert uv["with"]["save-cache"] == "${{ matrix.shard == 1 }}"


def test_unit_matrix_runs_one_shard_per_split():
    command = _pytest_command()
    splits = int(re.search(r"--splits (\d+)", command).group(1))
    workflow = yaml.safe_load((_ROOT / ".github/workflows/test.yml").read_text())
    assert splits > 1
    assert workflow["jobs"]["unit"]["strategy"]["matrix"]["shard"] == list(range(1, splits + 1))
    assert "--group ${{ matrix.shard }}" in command


def test_tests_run_on_pull_requests_into_dev_and_main():
    triggers = yaml.safe_load((_ROOT / ".github/workflows/test.yml").read_text())[True]
    assert set(triggers["pull_request"]["branches"]) == {"dev", "main"}
    assert triggers["push"]["branches"] == ["dev"]


def test_ruff_runs_in_the_tests_workflow_only():
    workflows = sorted((_ROOT / ".github/workflows").glob("*.yml"))
    assert [w.name for w in workflows if "ruff" in w.read_text()] == ["test.yml"]


@pytest.mark.parametrize("doc", ["README.md", "index.md"])
def test_workflow_badges_point_at_existing_workflows(doc):
    names = re.findall(r"actions/workflows/([\w.-]+\.yml)", (_ROOT / doc).read_text())
    assert names
    assert all((_ROOT / ".github/workflows" / name).is_file() for name in names), names


def _workflow() -> dict:
    return yaml.safe_load((_ROOT / ".github/workflows/test.yml").read_text())


def _gate_job() -> dict:
    return _workflow()["jobs"]["already-tested"]


def _artifact(expired=False, fork=False) -> dict:
    return {"expired": expired, "workflow_run": {"repository_id": 1, "head_repository_id": 2 if fork else 1}}


def _run_gate(tmp_path, listing: dict | None) -> str:
    (step,) = [s for s in _gate_job()["steps"] if "run" in s]
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fixture = tmp_path / "listing.json"
    fixture.write_text(json.dumps(listing))
    fake_gh = bin_dir / "gh"
    fake_gh.write_text(
        "#!/usr/bin/env bash\n"
        f'[ "{listing is None}" = True ] && exit 1\n'
        'while [ $# -gt 0 ]; do [ "$1" = --jq ] && f="$2"; shift; done\n'
        f'jq -r "$f" "{fixture}"\n'
    )
    fake_gh.chmod(0o755)
    out = tmp_path / "out"
    out.touch()
    env = {
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "GITHUB_OUTPUT": str(out),
        "GITHUB_REPOSITORY": "o/r",
        **{k: "abc123" for k in step.get("env", {})},
    }
    subprocess.run(["bash", "-e", "-c", step["run"]], env=env, check=True)
    return out.read_text()


@pytest.mark.parametrize(
    ("listing", "skip"),
    [
        ({"artifacts": [_artifact()]}, "true"),
        ({"artifacts": [_artifact(expired=True)]}, "false"),
        ({"artifacts": [_artifact(fork=True)]}, "false"),
        ({"artifacts": []}, "false"),
        (None, "false"),
    ],
)
def test_gate_skips_only_for_a_live_same_repo_pass_of_the_tree(tmp_path, listing, skip):
    assert _run_gate(tmp_path, listing) == f"skip={skip}\n"


def test_gate_runs_on_dev_pushes_and_looks_up_the_pushed_tree():
    job = _gate_job()
    assert job["if"] == "github.event_name == 'push'"
    assert job["permissions"]["actions"] == "read"
    assert job["outputs"]["skip"] == "${{ steps.lookup.outputs.skip }}"
    (step,) = [s for s in job["steps"] if "run" in s]
    assert step["id"] == "lookup"
    assert step["env"]["TREE"] == "${{ github.event.head_commit.tree_id }}"
    assert "name=tests-passed-$TREE" in step["run"]


@pytest.mark.parametrize("job", ["unit", "lint"])
def test_unit_and_lint_skip_when_the_tree_already_passed(job):
    spec = _workflow()["jobs"][job]
    assert spec["needs"] == "already-tested"
    assert spec["if"] == "${{ !cancelled() && needs.already-tested.outputs.skip != 'true' }}"


def test_pull_requests_record_the_tested_tree_after_unit_and_lint_pass():
    job = _workflow()["jobs"]["record-pass"]
    assert job["needs"] == ["unit", "lint"]
    assert job["if"] == (
        "${{ !cancelled() && github.event_name == 'pull_request'"
        " && needs.unit.result == 'success' && needs.lint.result == 'success' }}"
    )
    tree, upload = job["steps"]
    assert tree["env"]["GH_TOKEN"] == "${{ github.token }}"
    assert "git/commits/$GITHUB_SHA" in tree["run"]
    assert upload["uses"].startswith("actions/upload-artifact@")
    assert upload["with"]["name"] == "tests-passed-${{ steps.tree.outputs.sha }}"


def _warm_step(predicate) -> dict:
    (step,) = [s for s in _workflow()["jobs"]["warm-cache"]["steps"] if predicate(s)]
    return step


def test_warm_cache_runs_only_when_the_dev_push_skips_its_tests():
    job = _workflow()["jobs"]["warm-cache"]
    assert job["needs"] == "already-tested"
    assert job["if"] == "${{ !cancelled() && needs.already-tested.outputs.skip == 'true' }}"


def test_warm_cache_covers_every_unit_python_version():
    jobs = _workflow()["jobs"]
    assert (
        jobs["warm-cache"]["strategy"]["matrix"]["python-version"]
        == jobs["unit"]["strategy"]["matrix"]["python-version"]
    )
    (step,) = [s for s in jobs["warm-cache"]["steps"] if s.get("uses", "").startswith("actions/setup-python")]
    assert step["with"]["python-version"] == "${{ matrix.python-version }}"
    assert step["uses"] == _setup_python_steps("unit")[0]["uses"]


def test_warm_cache_writes_the_key_the_unit_shards_restore():
    _, unit_uv = _unit_step_index(lambda s: s.get("uses", "").startswith("astral-sh/setup-uv"))
    uv = _warm_step(lambda s: s.get("uses", "").startswith("astral-sh/setup-uv"))
    assert uv["uses"] == unit_uv["uses"]
    assert uv["with"]["enable-cache"] is True
    assert uv["with"]["cache-dependency-glob"] == unit_uv["with"]["cache-dependency-glob"]
    assert "save-cache" not in uv["with"]


def test_warm_cache_installs_only_on_a_cache_miss():
    uv = _warm_step(lambda s: s.get("uses", "").startswith("astral-sh/setup-uv"))
    install = _warm_step(lambda s: s.get("name") == "Install dependencies")
    _, unit_install = _unit_step_index(lambda s: s.get("name") == "Install dependencies")
    assert install["if"] == f"steps.{uv['id']}.outputs.cache-hit != 'true'"
    assert install["run"] == unit_install["run"]
