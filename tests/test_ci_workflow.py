import json
import os
import re
import subprocess
import tomllib
from importlib.metadata import entry_points
from pathlib import Path

import pytest
import yaml
from _pytest.config import default_plugins

from tests import refresh_durations
from tests.conftest import COLLECTED_NODEIDS
from tests.refresh_durations import ci_run_ids, ci_samples, median_durations

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


def test_workflow_keeps_xdist_groups_on_one_worker():
    assert "--dist loadgroup" in _pytest_command()


def test_unit_shards_measure_no_coverage():
    command = _pytest_command()
    assert "--cov" not in command
    assert "matrix.cov" not in command
    assert "include" not in _workflow()["jobs"]["unit"]["strategy"]["matrix"]
    _, step = _unit_step_index(lambda s: s.get("name") == "Run tests")
    assert "COVERAGE_CORE" not in step.get("env", {})


def test_unit_shards_block_only_real_plugins():
    blocked = set(re.findall(r"-p no:(\S+)", _pytest_command()))
    known = {entry.name for entry in entry_points(group="pytest11")} | set(default_plugins)
    assert blocked
    assert blocked <= known


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


def test_unit_installs_extras_with_uv_and_no_uv_cache():
    uv_index, uv = _unit_step_index(lambda s: s.get("uses", "").startswith("astral-sh/setup-uv"))
    install_index, install = _unit_step_index(lambda s: s.get("name") == "Install dependencies")
    assert uv["with"]["enable-cache"] is False
    assert uv_index < install_index
    assert install["run"].strip() == 'uv pip install --system --excludes .github/test-excludes.txt -e ".[dev,all]"'


def test_unit_install_excludes_playwright_and_the_grpc_exporter():
    excludes = (_ROOT / ".github/test-excludes.txt").read_text().split()
    assert excludes == ["playwright", "opentelemetry-exporter-otlp-proto-grpc"]


def test_unit_matrix_runs_one_shard_per_split():
    command = _pytest_command()
    shards = int(re.search(r"--shard \$\{\{ matrix\.shard \}\}/(\d+)", command).group(1))
    workflow = yaml.safe_load((_ROOT / ".github/workflows/test.yml").read_text())
    assert shards > 1
    assert workflow["jobs"]["unit"]["strategy"]["matrix"]["shard"] == list(range(1, shards + 1))
    assert "--splits" not in command


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


def _lookup_step(job: str = "unit") -> dict:
    return _workflow()["jobs"][job]["steps"][0]


def _artifact(expired=False, fork=False) -> dict:
    return {"expired": expired, "workflow_run": {"repository_id": 1, "head_repository_id": 2 if fork else 1}}


def _run_gate(tmp_path, listing: dict | None) -> str:
    step = _lookup_step()
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


def test_no_separate_gate_job_delays_the_shards():
    jobs = _workflow()["jobs"]
    assert "already-tested" not in jobs
    assert "needs" not in jobs["unit"]
    assert "needs" not in jobs["lint"]


@pytest.mark.parametrize("job", ["unit", "lint"])
def test_each_job_looks_up_the_pushed_tree_first(job):
    spec = _workflow()["jobs"][job]
    assert spec["permissions"] == {"contents": "read", "actions": "read"}
    step = _lookup_step(job)
    assert step == _lookup_step("unit")
    assert step["id"] == "lookup"
    assert step["if"] == "github.event_name == 'push'"
    assert step["env"]["TREE"] == "${{ github.event.head_commit.tree_id }}"
    assert "name=tests-passed-$TREE" in step["run"]


@pytest.mark.parametrize("job", ["unit", "lint"])
def test_every_later_step_skips_when_the_tree_already_passed(job):
    later = _workflow()["jobs"][job]["steps"][1:]
    assert later
    assert all(s.get("if") == "steps.lookup.outputs.skip != 'true'" for s in later), later


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


def test_unit_pins_an_exact_uv_version():
    _, uv = _unit_step_index(lambda s: s.get("uses", "").startswith("astral-sh/setup-uv"))
    assert re.fullmatch(r"\d+\.\d+\.\d+", uv["with"]["version"])


def test_stored_durations_cover_the_collected_suite(request):
    collected = request.config.stash[COLLECTED_NODEIDS]
    stored = json.loads((_ROOT / ".test_durations").read_text())
    missing = [nodeid for nodeid in collected if nodeid.split("@", 1)[0] not in stored]
    assert len(missing) * 10 <= len(collected), (
        f"{len(missing)} of {len(collected)} tests have no stored duration; "
        "refresh them with: python -m tests.refresh_durations"
    )


def test_refreshed_durations_take_the_median_so_one_slow_run_does_not_move_a_test():
    runs = [{"a": 0.1, "b": 1.0}, {"a": 2.5, "b": 1.2}, {"a": 0.2, "b": 1.1}]
    assert median_durations(runs) == {"a": 0.2, "b": 1.1}


def test_unit_shards_store_the_durations_they_measure():
    command = _pytest_command()
    assert "--store-durations --durations-path durations.json" in command
    assert "no:pytest-split" not in command


def test_unit_shards_upload_their_durations_for_the_refresh():
    upload_index, upload = _unit_step_index(lambda s: s.get("name") == "Upload durations")
    run_index, _ = _unit_step_index(lambda s: s.get("name") == "Run tests")
    assert upload_index == run_index + 1
    assert upload["if"] == "steps.lookup.outputs.skip != 'true'"
    assert upload["uses"].startswith("actions/upload-artifact@")
    assert upload["with"]["name"] == "durations-${{ matrix.python-version }}-${{ matrix.shard }}"
    assert upload["with"]["path"] == "durations.json"


def test_ci_samples_are_one_per_shard_file_without_the_xdist_group_suffix(tmp_path):
    shards = {
        "durations-3.11-1": {"t.py::a@fakeredis": 1.0},
        "durations-3.12-1": {"t.py::a@fakeredis": 3.0, "t.py::b[x@y]": 2.0},
    }
    for name, durations in shards.items():
        (tmp_path / "7" / name).mkdir(parents=True)
        (tmp_path / "7" / name / "durations.json").write_text(json.dumps(durations))
    assert ci_samples(tmp_path) == [{"t.py::a": 1.0}, {"t.py::a": 3.0, "t.py::b[x@y]": 2.0}]


def test_ci_refresh_reads_the_newest_distinct_runs_that_kept_durations():
    calls = []

    def gh(args):
        calls.append(args)
        return "9\n9\n5\n3\n"

    assert ci_run_ids(2, gh) == ["9", "5"]
    assert "actions/artifacts?name=durations-3.12-1" in calls[0][1]


def test_ci_refresh_stores_the_median_of_the_downloaded_shard_files(tmp_path, monkeypatch):
    def download(run_ids, folder):
        for run, seconds in zip(run_ids, (1.0, 5.0, 2.0)):
            (folder / run / "durations-3.12-1").mkdir(parents=True)
            (folder / run / "durations-3.12-1" / "durations.json").write_text(json.dumps({"t.py::a@g": seconds}))

    monkeypatch.setattr(refresh_durations, "_ROOT", tmp_path)
    monkeypatch.setattr(refresh_durations, "ci_run_ids", lambda limit: ["1", "2", "3"][:limit])
    monkeypatch.setattr(refresh_durations, "ci_download", download)
    refresh_durations.main(["--ci", "3"])
    assert json.loads((tmp_path / ".test_durations").read_text()) == {"t.py::a": 2.0}


def test_mutation_job_runs_independently_and_keeps_its_evidence():
    spec = yaml.safe_load((_ROOT / ".github/workflows/mutation.yml").read_text())
    job = spec["jobs"]["mutation"]
    assert "needs" not in job
    assert job["timeout-minutes"] == 20
    steps = job["steps"]
    checkout = next(step for step in steps if step.get("uses", "").startswith("actions/checkout"))
    assert checkout["with"]["fetch-depth"] == 0
    assert checkout["with"]["ref"] == "${{ github.event.pull_request.head.sha }}"
    run = next(step for step in steps if step.get("name") == "Mutate changed Python files")
    assert run["env"]["BASE"] == "${{ github.event.pull_request.base.sha }}"
    assert run["run"] == 'python -m scripts.ci_mutation --base "$BASE" --budget 1080'
    artifact = next(step for step in steps if step.get("uses", "").startswith("actions/upload-artifact"))
    assert artifact["if"] == "always()"
    assert artifact["with"]["include-hidden-files"] is True
