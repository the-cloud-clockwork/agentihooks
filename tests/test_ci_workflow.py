import json
import os
import re
import subprocess
import sys
import tomllib
from importlib.metadata import entry_points
from pathlib import Path

import pytest
import yaml
from _pytest.config import default_plugins

from tests import refresh_durations
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
    assert install["run"].strip().splitlines() == [
        'uv venv --python "${{ steps.python.outputs.python-path }}" "$HOME/venv"',
        'uv pip install --python "$HOME/venv/bin/python" --excludes .github/test-excludes.txt -e ".[dev,all]"',
    ]


def test_unit_restores_one_venv_per_interpreter_and_dependency_files():
    _, cache = _unit_step_index(lambda s: s.get("uses", "").startswith("actions/cache@"))
    _, python = _unit_step_index(lambda s: s.get("uses", "").startswith("actions/setup-python"))
    key = cache["with"]["key"]
    assert cache["with"]["path"] == "~/venv"
    assert "${{ runner.os }}" in key
    assert f"${{{{ steps.{python['id']}.outputs.python-version }}}}" in key
    assert "${{ hashFiles('pyproject.toml', '.github/test-excludes.txt') }}" in key
    _, day = _unit_step_index(lambda s: s.get("id") == "day")
    assert day["run"] == 'echo "date=$(date -u +%F)" >> "$GITHUB_OUTPUT"'
    assert "${{ steps.day.outputs.date }}" in key
    assert "matrix.shard" not in key


def test_a_restored_venv_skips_uv_and_the_install():
    _, cache = _unit_step_index(lambda s: s.get("uses", "").startswith("actions/cache@"))
    hit = f"steps.{cache['id']}.outputs.cache-hit != 'true'"
    for predicate in (
        lambda s: s.get("uses", "").startswith("astral-sh/setup-uv"),
        lambda s: s.get("name") == "Install dependencies",
    ):
        _, step = _unit_step_index(predicate)
        assert step["if"] == hit


def test_unit_tests_run_from_the_venv():
    path_index, path = _unit_step_index(lambda s: "GITHUB_PATH" in s.get("run", ""))
    run_index, _ = _unit_step_index(lambda s: s.get("name") == "Run tests")
    assert path["run"].strip() == 'echo "$HOME/venv/bin" >> "$GITHUB_PATH"'
    assert path_index < run_index


def test_unit_install_keeps_playwright_and_excludes_the_grpc_exporter():
    excludes = (_ROOT / ".github/test-excludes.txt").read_text().split()
    assert excludes == ["opentelemetry-exporter-otlp-proto-grpc"]


def test_unit_matrix_runs_one_shard_per_split():
    command = _pytest_command()
    split = r"--shard \$\{\{ matrix\.shard \}\}/\$\{\{ matrix\.python-version == '3\.12' && (\d+) \|\| (\d+) \}\}"
    coverage_shards, plain_shards = (int(count) for count in re.search(split, command).groups())
    workflow = yaml.safe_load((_ROOT / ".github/workflows/test.yml").read_text())
    matrix = workflow["jobs"]["unit"]["strategy"]["matrix"]
    excluded = {(entry["python-version"], entry["shard"]) for entry in matrix.get("exclude", [])}
    counts = {"3.11": plain_shards, "3.12": coverage_shards}
    for version in matrix["python-version"]:
        assert [shard for shard in matrix["shard"] if (version, shard) not in excluded] == list(
            range(1, counts[version] + 1)
        )
    assert coverage_shards > plain_shards > 1
    merge = next(step for step in workflow["jobs"]["sonar"]["steps"] if step.get("name") == "Merge shard coverage")
    assert merge["run"].split()[-1] == str(coverage_shards)
    assert "--splits" not in command


def test_a_hung_unit_shard_fails_near_twice_the_slowest_shard():
    slowest_shard_minutes = 186 / 60
    timeout = _workflow()["jobs"]["unit"]["timeout-minutes"]
    assert 2 * slowest_shard_minutes <= timeout <= 3 * slowest_shard_minutes


def test_tests_run_on_pull_requests_into_dev_and_main():
    triggers = yaml.safe_load((_ROOT / ".github/workflows/test.yml").read_text())[True]
    assert set(triggers["pull_request"]["branches"]) == {"dev", "main"}
    assert triggers["push"]["branches"] == ["dev"]
    assert triggers["merge_group"] == {"types": ["checks_requested"]}


def test_ruff_runs_in_the_tests_workflow_only():
    workflows = sorted((_ROOT / ".github/workflows").glob("*.yml"))
    assert [w.name for w in workflows if "ruff" in w.read_text()] == ["test.yml"]


def _browser_install(steps):
    return next(
        step
        for step in steps
        if "playwright install --with-deps chromium" in step.get("run", "")
        or step.get("uses", "").endswith("/.github/actions/browser-cache")
    )


def _browser_setup_steps(steps):
    install = _browser_install(steps)
    if "uses" in install:
        action = yaml.safe_load((_ROOT / ".github/actions/browser-cache/action.yml").read_text())
        return action["runs"]["steps"]
    return steps


def test_lint_runs_the_artifact_sanity_checks_in_a_real_browser():
    steps = _workflow()["jobs"]["lint"]["steps"]
    install = _browser_install(steps)
    check = next(s for s in steps if s.get("run", "").endswith(".artifact_sanity tests/fixtures/artifacts/*"))
    assert steps.index(install) < steps.index(check)
    assert {p.suffix for p in (_ROOT / "tests/fixtures/artifacts").iterdir()} == {".md", ".json", ".svg"}


def test_queued_merges_select_artifact_checks_against_the_queue_base():
    select = next(s for s in _workflow()["jobs"]["lint"]["steps"] if s.get("id") == "artifacts")
    assert select["env"]["BASE"] == (
        "${{ github.event.pull_request.base.sha || github.event.merge_group.base_sha"
        " || github.event.before || inputs.base }}"
    )


def _mutation_workflow():
    separate = _ROOT / ".github/workflows/mutation.yml"
    path = separate if separate.is_file() else _ROOT / ".github/workflows/test.yml"
    return yaml.safe_load(path.read_text())


def test_mutation_job_installs_chromium_before_mutating():
    steps = _mutation_workflow()["jobs"]["mutation"]["steps"]
    names = [s.get("name") for s in steps]
    install = _browser_install(steps)
    assert install["name"] == "Install the browser that page tests drive"
    if "run" in install:
        assert install["run"] == "python -m playwright install --with-deps chromium"
    assert names.index("Install dependencies") < steps.index(install) < names.index("Mutate changed Python files")


def test_mutation_browser_setup_is_selected_bounded_and_reports_failure():
    steps = _mutation_workflow()["jobs"]["mutation"]["steps"]
    names = [step.get("name") for step in steps]
    select = names.index("Select mutation tests before browser setup")
    install = names.index("Install the browser that page tests drive")
    assert select < install
    assert steps[select]["id"] == "selection"
    assert steps[select]["run"].startswith("python -m scripts.ci_mutation." + "browser ")
    assert steps[install]["if"] == "steps.selection.outputs.browser == 'true'"
    assert steps[install]["id"] == "browser"
    assert steps[install]["timeout-minutes"] == 2
    job = _mutation_workflow()["jobs"]["mutation"]
    assert job["env"]["PLAYWRIGHT_BROWSERS_PATH"] == "${{ github.workspace }}/.playwright"
    failure = next(step for step in steps if step.get("name") == "Report browser setup failure")
    assert failure["if"] == "failure() && steps.browser.outcome == 'failure'"
    assert "::error::" in failure["run"]
    assert "two minute" in failure["run"]
    assert "exit 1" in failure["run"]


def test_mutation_browser_dependencies_use_the_responsive_mirror():
    steps = _mutation_workflow()["jobs"]["mutation"]["steps"]
    setup = _browser_setup_steps(steps)
    mirror = next(step for step in setup if step.get("name") == "Use the Ubuntu archive for browser dependencies")
    install = _browser_install(setup)
    assert setup.index(mirror) < setup.index(install)
    assert mirror["if"] in ("steps.selection.outputs.browser == 'true'", "steps.launch.outputs.ready != 'true'")
    assert mirror["run"] in (
        r"sudo sed -i '/azure\.archive\.ubuntu\.com/d' /etc/apt/apt-mirrors.txt",
        r"timeout 60s sudo sed -i '/azure\.archive\.ubuntu\.com/d' /etc/apt/apt-mirrors.txt",
    )
    assert mirror.get("timeout-minutes") == 1 or mirror["run"].startswith("timeout 60s ")


def test_lint_and_equivalence_browser_installs_have_the_same_timeout():
    lint = yaml.safe_load((_ROOT / ".github/workflows/test.yml").read_text())["jobs"]["lint"]["steps"]
    equivalence = yaml.safe_load((_ROOT / ".github/workflows/equivalence.yml").read_text())["jobs"][
        "ledger-equivalence"
    ]["steps"]
    for steps in (lint, equivalence):
        assert _browser_install(steps)["timeout-minutes"] == 2


def test_lint_and_equivalence_browser_setup_uses_the_working_mirror():
    lint = yaml.safe_load((_ROOT / ".github/workflows/test.yml").read_text())["jobs"]["lint"]["steps"]
    equivalence = yaml.safe_load((_ROOT / ".github/workflows/equivalence.yml").read_text())["jobs"][
        "ledger-equivalence"
    ]["steps"]
    for steps in (lint, equivalence):
        setup = _browser_setup_steps(steps)
        mirror = next(step for step in setup if step.get("name") == "Use the Ubuntu archive for browser dependencies")
        assert setup.index(mirror) < setup.index(_browser_install(setup))
        assert mirror["run"] in (
            r"sudo sed -i '/azure\.archive\.ubuntu\.com/d' /etc/apt/apt-mirrors.txt",
            r"timeout 60s sudo sed -i '/azure\.archive\.ubuntu\.com/d' /etc/apt/apt-mirrors.txt",
        )
        assert mirror.get("timeout-minutes") == 1 or mirror["run"].startswith("timeout 60s ")
    install = _browser_install(lint)
    if "run" in install:
        mirror = next(step for step in lint if step.get("name") == "Use the Ubuntu archive for browser dependencies")
        assert "if" not in mirror
    else:
        assert install["if"] == "steps.artifacts.outputs.browser == 'true'"


def test_equivalence_runs_storage_then_page_replays_each_group_at_once():
    steps = yaml.safe_load((_ROOT / ".github/workflows/equivalence.yml").read_text())["jobs"]["ledger-equivalence"][
        "steps"
    ]
    store, pages = [step for step in steps if "_replay.py" in step.get("run", "")]
    assert store["run"].count("repository_replay.py") == 3 and "page_replay.py" not in store["run"]
    assert pages["run"].count("page_replay.py") == 3 and "repository_replay.py" not in pages["run"]
    for step in (store, pages):
        run = step["run"]
        assert run.count(" &\n") == 3
        assert "|| failed=1" in run and 'exit "$failed"' in run
        assert 'cat "$RUNNER_TEMP/$name.log"' in run and "return 1" in run
    assert steps.index(store) < steps.index(_browser_install(steps)) < steps.index(pages)
    assert all("Path('head/" not in step.get("run", "") for step in steps)


@pytest.mark.parametrize("doc", ["README.md", "index.md"])
def test_workflow_badges_point_at_existing_workflows(doc):
    names = re.findall(r"actions/workflows/([\w.-]+\.yml)", (_ROOT / doc).read_text())
    assert names
    assert all((_ROOT / ".github/workflows" / name).is_file() for name in names), names


def _workflow() -> dict:
    return yaml.safe_load((_ROOT / ".github/workflows/test.yml").read_text())


def test_no_separate_gate_job_delays_the_shards():
    jobs = _workflow()["jobs"]
    assert "already-tested" not in jobs
    assert "needs" not in jobs["unit"]
    assert "needs" not in jobs["lint"]


def test_unit_pins_an_exact_uv_version():
    _, uv = _unit_step_index(lambda s: s.get("uses", "").startswith("astral-sh/setup-uv"))
    assert re.fullmatch(r"\d+\.\d+\.\d+", uv["with"]["version"])


def test_stored_durations_cover_the_collected_suite():
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "tests/", "--collect-only", "-q", "-n", "0", "-o", "addopts="],
        cwd=_ROOT,
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, "PYTEST_ADDOPTS": "", "PYTEST_DISABLE_PLUGIN_AUTOLOAD": ""},
    )
    assert result.returncode == 0, result.stdout + result.stderr
    collected = [line for line in result.stdout.splitlines() if line.startswith("tests/") and "::" in line]
    assert collected, result.stdout
    stored = json.loads((_ROOT / ".test_durations").read_text())
    missing = [nodeid for nodeid in collected if re.sub(r"@[^\[\]]*$", "", nodeid) not in stored]
    assert len(missing) * 10 <= len(collected), (
        f"{len(missing)} of {len(collected)} tests have no stored duration; "
        "refresh them with: python -m tests.refresh_durations"
    )


@pytest.mark.parametrize("missing", [0, 5, 10, 11, 100])
def test_stored_durations_allow_new_tests_concentrated_in_one_shard(tmp_path, monkeypatch, missing):
    nodeids = [f"tests/test_probe.py::test_{i}" for i in range(100)]
    (tmp_path / ".test_durations").write_text(json.dumps(dict.fromkeys(nodeids[: 100 - missing], 0.1)))
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests/test_probe.py").write_text("\n".join(f"def test_{i}(): pass" for i in range(100)))
    monkeypatch.setattr(sys.modules[__name__], "_ROOT", tmp_path)
    monkeypatch.setenv("PYTEST_ADDOPTS", "--shard 1/4")
    monkeypatch.setenv("PYTEST_DISABLE_PLUGIN_AUTOLOAD", "1")
    if missing <= 10:
        test_stored_durations_cover_the_collected_suite()
    else:
        with pytest.raises(AssertionError, match=f"{missing} of 100 tests"):
            test_stored_durations_cover_the_collected_suite()


def test_dev_push_refreshes_stored_durations_after_tests_pass():
    job = _workflow()["jobs"]["refresh-durations"]
    assert job["needs"] == ["unit", "lint"]
    assert job["if"] == "github.event_name == 'push'"
    assert job["permissions"] == {"contents": "read"}
    steps = job["steps"]
    checkout = next(step for step in steps if step.get("uses") == "actions/checkout@v4")
    assert checkout["with"]["ref"] == "${{ github.sha }}"
    restore = next(step for step in steps if step.get("name") == "Restore recent shard durations")
    download = next(step for step in steps if step.get("name") == "Download this run's shard durations")
    refresh = next(step for step in steps if step.get("name") == "Refresh measured durations")
    save = next(step for step in steps if step.get("name") == "Save recent shard durations")
    assert steps.index(restore) < steps.index(download) < steps.index(refresh) < steps.index(save)
    assert restore["uses"].startswith("actions/cache/restore@")
    assert restore["with"] == {
        "path": "~/durations-samples",
        "key": "durations-samples-${{ github.sha }}",
        "restore-keys": "durations-samples-",
    }
    assert download["uses"].startswith("actions/download-artifact@")
    assert download["with"] == {"pattern": "durations-3.*", "path": "~/durations-samples/${{ github.run_id }}"}
    assert (
        refresh["run"]
        == 'python -m tests.refresh_durations --samples ~/durations-samples --ci-run "$GITHUB_RUN_ID" --ci 5\ntest -s .test_durations\n'
    )
    assert "env" not in refresh
    assert save["uses"].startswith("actions/cache/save@")
    assert save["with"] == {"path": "~/durations-samples", "key": "durations-samples-${{ github.sha }}"}


def _sample(folder: Path, run: str, seconds: float) -> None:
    for version in ("3.11", "3.12"):
        path = folder / run / f"durations-{version}-1"
        path.mkdir(parents=True)
        (path / "durations.json").write_text(json.dumps({"t.py::a@g": seconds}))


def test_samples_refresh_keeps_the_newest_runs_and_never_calls_github(tmp_path, monkeypatch):
    samples = tmp_path / "samples"
    for run, seconds in [("8", 9.0), ("9", 9.0), ("10", 1.0), ("11", 2.0), ("12", 3.0)]:
        _sample(samples, run, seconds)

    def offline(*args):
        raise AssertionError("the samples refresh reached GitHub")

    monkeypatch.setattr(refresh_durations, "_ROOT", tmp_path)
    monkeypatch.setattr(refresh_durations, "collected_tests", lambda root: ["t.py::a"])
    monkeypatch.setattr(refresh_durations, "ci_run_ids", offline)
    monkeypatch.setattr(refresh_durations, "ci_download", offline)
    refresh_durations.main(["--samples", str(samples), "--ci-run", "12", "--ci", "3"])
    assert sorted(path.name for path in samples.iterdir()) == ["10", "11", "12"]
    assert json.loads((tmp_path / ".test_durations").read_text()) == {"t.py::a": 2.0}


def test_samples_refresh_keeps_a_rerun_older_than_the_newest_runs(tmp_path, monkeypatch):
    samples = tmp_path / "samples"
    for run, seconds in [("5", 4.0), ("10", 1.0), ("11", 2.0), ("12", 3.0)]:
        _sample(samples, run, seconds)
    monkeypatch.setattr(refresh_durations, "_ROOT", tmp_path)
    monkeypatch.setattr(refresh_durations, "collected_tests", lambda root: ["t.py::a"])
    refresh_durations.main(["--samples", str(samples), "--ci-run", "5", "--ci", "3"])
    assert sorted(path.name for path in samples.iterdir()) == ["11", "12", "5"]
    assert json.loads((tmp_path / ".test_durations").read_text()) == {"t.py::a": 3.0}


def test_samples_under_the_run_limit_are_all_kept(tmp_path):
    samples = tmp_path / "samples"
    for run in ("3", "20", "100"):
        (samples / run).mkdir(parents=True)
    refresh_durations.keep_newest(samples, 5, "100")
    assert sorted(path.name for path in samples.iterdir()) == ["100", "20", "3"]


def test_a_foreign_folder_among_the_samples_never_fails_the_pruning(tmp_path):
    samples = tmp_path / "samples"
    for run in ("notes", "1", "2", "3"):
        (samples / run).mkdir(parents=True)
    refresh_durations.keep_newest(samples, 2, "3")
    assert sorted(path.name for path in samples.iterdir()) == ["2", "3", "notes"]


def test_samples_refresh_refuses_a_run_that_kept_no_durations(tmp_path, monkeypatch):
    samples = tmp_path / "samples"
    _sample(samples, "11", 1.0)
    monkeypatch.setattr(refresh_durations, "_ROOT", tmp_path)
    monkeypatch.setattr(refresh_durations, "collected_tests", lambda root: ["t.py::a"])
    with pytest.raises(SystemExit, match="run 12 kept no durations"):
        refresh_durations.main(["--samples", str(samples), "--ci-run", "12", "--ci", "5"])


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
    assert "if" not in upload
    assert upload["uses"].startswith("actions/upload-artifact@")
    assert upload["with"]["name"] == "durations-${{ matrix.python-version }}-${{ matrix.shard }}"
    assert upload["with"]["path"].split() == ["durations.json", "durations.sha256"]


def test_every_shard_of_a_run_judges_the_restored_durations_by_one_event_time_and_records_their_hash():
    _, adopt = _unit_step_index(lambda s: s.get("name") == "Adopt latest dev durations")
    assert adopt["env"]["RUN_TIME"] == (
        "${{ github.event.pull_request.updated_at || github.event.merge_group.head_commit.timestamp"
        " || github.event.repository.pushed_at }}"
    )
    assert '--run-time "$RUN_TIME" --hash durations.sha256' in adopt["run"]
    steps = [step.get("name") for step in _workflow()["jobs"]["refresh-durations"]["steps"]]
    stage = _workflow()["jobs"]["refresh-durations"]["steps"][steps.index("Stage merged durations for the cache")]
    assert "date +%s > ~/dev-durations/saved-at" in stage["run"]
    assert steps.index("Save merged durations to the cache") == steps.index(stage["name"]) + 1


def test_ci_samples_are_one_per_shard_file_without_the_xdist_group_suffix(tmp_path):
    shards = {
        "durations-3.11-1": {"t.py::a@group": 1.0},
        "durations-3.12-1": {"t.py::a@group": 3.0, "t.py::b[x@y]": 2.0},
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


def test_ci_refresh_lists_same_repository_runs_newest_first():
    def artifact(run, created, head_repository=1, expired=False):
        return {
            "expired": expired,
            "created_at": created,
            "workflow_run": {"id": run, "repository_id": 1, "head_repository_id": head_repository},
        }

    listing = {
        "artifacts": [
            artifact(3, "2026-10-08T01:00:00Z"),
            artifact(9, "2026-10-08T09:00:00Z"),
            artifact(7, "2026-10-08T08:00:00Z", head_repository=2),
            artifact(8, "2026-10-08T08:30:00Z", expired=True),
            artifact(5, "2026-10-08T05:00:00Z"),
        ]
    }

    def gh(args):
        jq = args[args.index("--jq") + 1]
        return subprocess.run(
            ["jq", "-r", jq], input=json.dumps(listing), capture_output=True, text=True, check=True
        ).stdout

    assert ci_run_ids(5, gh) == ["9", "5", "3"]


def test_ci_refresh_stores_the_median_of_the_downloaded_shard_files(tmp_path, monkeypatch):
    def download(run_ids, folder):
        for run, seconds in zip(run_ids, (1.0, 5.0, 2.0)):
            for version in ("3.11", "3.12"):
                path = folder / run / f"durations-{version}-1"
                path.mkdir(parents=True)
                (path / "durations.json").write_text(json.dumps({"t.py::a@g": seconds}))

    monkeypatch.setattr(refresh_durations, "_ROOT", tmp_path)
    monkeypatch.setattr(refresh_durations, "collected_tests", lambda root: ["t.py::a"])
    monkeypatch.setattr(refresh_durations, "ci_run_ids", lambda limit: ["1", "2", "3"][:limit])
    monkeypatch.setattr(refresh_durations, "ci_download", download)
    refresh_durations.main(["--ci", "3"])
    assert json.loads((tmp_path / ".test_durations").read_text()) == {"t.py::a": 2.0}


def test_ci_refresh_can_use_the_exact_run_that_passed_the_dev_tree(tmp_path, monkeypatch):
    def download(run_ids, folder):
        assert run_ids == ["42"]
        for version, seconds in [("3.11", 1.0), ("3.12", 3.0)]:
            path = folder / "42" / f"durations-{version}-1"
            path.mkdir(parents=True)
            (path / "durations.json").write_text(json.dumps({"t.py::a": seconds}))

    monkeypatch.setattr(refresh_durations, "_ROOT", tmp_path)
    monkeypatch.setattr(refresh_durations, "collected_tests", lambda root: ["t.py::a"])
    monkeypatch.setattr(refresh_durations, "ci_download", download)
    refresh_durations.main(["--ci-run", "42"])
    assert json.loads((tmp_path / ".test_durations").read_text()) == {"t.py::a": 2.0}
    assert json.loads((tmp_path / ".test_durations-3.11").read_text()) == {"t.py::a": 1.0}
    assert json.loads((tmp_path / ".test_durations-3.12").read_text()) == {"t.py::a": 3.0}


def test_ci_refresh_takes_the_median_over_recent_runs_for_the_tests_of_the_passed_run(tmp_path, monkeypatch):
    measured = {
        "42": {"t.py::a": 9.0, "t.py::new": 4.0},
        "41": {"t.py::a": 1.0, "t.py::gone": 7.0},
        "40": {"t.py::a": 2.0, "t.py::gone": 7.0},
    }

    def download(run_ids, folder):
        assert run_ids == ["42", "41", "40"]
        for run in run_ids:
            for version, scale in (("3.11", 1), ("3.12", 10)):
                path = folder / run / f"durations-{version}-1"
                path.mkdir(parents=True)
                (path / "durations.json").write_text(json.dumps({k: v * scale for k, v in measured[run].items()}))

    monkeypatch.setattr(refresh_durations, "_ROOT", tmp_path)
    monkeypatch.setattr(refresh_durations, "collected_tests", lambda root: ["t.py::a"])
    monkeypatch.setattr(refresh_durations, "ci_run_ids", lambda limit: ["42", "41", "40", "39"][:limit])
    monkeypatch.setattr(refresh_durations, "ci_download", download)
    refresh_durations.main(["--ci-run", "42", "--ci", "3"])
    assert json.loads((tmp_path / ".test_durations-3.11").read_text()) == {"t.py::a": 2.0, "t.py::new": 4.0}
    assert json.loads((tmp_path / ".test_durations-3.12").read_text()) == {"t.py::a": 20.0, "t.py::new": 40.0}
    assert json.loads((tmp_path / ".test_durations").read_text()) == {"t.py::a": 9.5, "t.py::new": 22.0}


def test_ci_refresh_counts_the_passed_run_within_its_run_limit(tmp_path, monkeypatch):
    downloads = []

    def download(run_ids, folder):
        downloads.extend(run_ids)
        for run in run_ids:
            for version in ("3.11", "3.12"):
                (folder / run / f"durations-{version}-1").mkdir(parents=True)
                (folder / run / f"durations-{version}-1" / "durations.json").write_text(json.dumps({"t.py::a": 1.0}))

    monkeypatch.setattr(refresh_durations, "_ROOT", tmp_path)
    monkeypatch.setattr(refresh_durations, "collected_tests", lambda root: ["t.py::a"])
    monkeypatch.setattr(refresh_durations, "ci_run_ids", lambda limit: ["42", "41", "40"][:limit])
    monkeypatch.setattr(refresh_durations, "ci_download", download)
    refresh_durations.main(["--ci-run", "39", "--ci", "3"])
    assert downloads == ["39", "42", "41"]


def test_ci_refresh_refuses_a_passed_run_that_kept_no_durations(tmp_path, monkeypatch):
    def download(run_ids, folder):
        (folder / "41" / "durations-3.11-1").mkdir(parents=True)
        (folder / "41" / "durations-3.11-1" / "durations.json").write_text(json.dumps({"t.py::a": 1.0}))

    monkeypatch.setattr(refresh_durations, "_ROOT", tmp_path)
    monkeypatch.setattr(refresh_durations, "collected_tests", lambda root: ["t.py::a"])
    monkeypatch.setattr(refresh_durations, "ci_run_ids", lambda limit: ["41"][:limit])
    monkeypatch.setattr(refresh_durations, "ci_download", download)
    with pytest.raises(SystemExit, match="run 42 kept no durations"):
        refresh_durations.main(["--ci-run", "42", "--ci", "2"])
    assert not (tmp_path / ".test_durations").exists()


def test_credential_parameters_have_readable_timing_identifiers():
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "tests/handoff/test_check.py",
            "--collect-only",
            "-q",
            "-n",
            "0",
            "-o",
            "addopts=",
        ],
        cwd=_ROOT,
        capture_output=True,
        text=True,
        check=True,
        env={**os.environ, "PYTEST_ADDOPTS": "", "PYTEST_DISABLE_PLUGIN_AUTOLOAD": ""},
    )
    identifiers = [line for line in result.stdout.splitlines() if "::test_a_credential_value_is_refused[" in line]
    assert identifiers == [
        "tests/handoff/test_check.py::test_a_credential_value_is_refused[github]",
        "tests/handoff/test_check.py::test_a_credential_value_is_refused[aws]",
    ]


def test_mutation_job_runs_independently_and_keeps_its_evidence():
    spec = _mutation_workflow()
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
