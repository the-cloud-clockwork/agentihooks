import ast
import hashlib
import json
import os
import re
import subprocess
import sys
import tarfile
import tomllib
from importlib.metadata import entry_points
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml
from _pytest.config import default_plugins

from scripts import ci_gate_queue
from tests import refresh_durations
from tests.refresh_durations import ci_run_ids, ci_samples, median_durations

pytestmark = pytest.mark.unit

_ROOT = Path(__file__).parent.parent
_EQUIVALENCE = _ROOT / "tests/swarm_ledger/equivalence.sh"


def _pytest_command() -> str:
    for line in (_ROOT / ".github/workflows/test.yml").read_text().splitlines():
        if "pytest" in line and line.strip().startswith("run:"):
            return line
    raise AssertionError("no pytest step in test.yml")


def _rearms_hung_test_dump(fixture: ast.FunctionDef) -> bool:
    autouse = any(
        isinstance(decorator, ast.Call)
        and any(keyword.arg == "autouse" and getattr(keyword.value, "value", False) for keyword in decorator.keywords)
        for decorator in fixture.decorator_list
    )
    return autouse and any(
        isinstance(node, ast.Attribute)
        and node.attr == "dump_traceback_later"
        and isinstance(node.value, ast.Name)
        and node.value.id == "faulthandler"
        for node in ast.walk(fixture)
    )


def _slowest_committed_test(skip_rearmed: bool = True) -> float:
    rearmed = {
        path.relative_to(_ROOT).as_posix()
        for path in (_ROOT / "tests").rglob("test_*.py")
        if skip_rearmed
        and any(
            isinstance(node, ast.FunctionDef) and _rearms_hung_test_dump(node)
            for node in ast.parse(path.read_text()).body
        )
    }
    return max(
        max(
            seconds
            for nodeid, seconds in json.loads(
                subprocess.check_output(["git", "show", f"HEAD:{name}"], cwd=_ROOT, text=True)
            ).items()
            if nodeid.split("::", 1)[0] not in rearmed
        )
        for name in (".test_durations", ".test_durations-3.12")
    )


@pytest.mark.parametrize("plugin", ["pytest-xdist", "pytest-split"])
def test_dev_extra_declares_test_plugin(plugin):
    dev = tomllib.loads((_ROOT / "pyproject.toml").read_text())["project"]["optional-dependencies"]["dev"]
    assert any(dep.startswith(plugin) for dep in dev)


def test_workflow_runs_whole_suite_in_parallel():
    command = _pytest_command()
    assert command.strip().startswith(
        (
            "run: workers=$(",
            "run: start=$SECONDS && status=0 && workers=$(",
            'run: if [[ -n "${QUEUE_SHARDS:-}" ]]; then SHARD="${SHARD%/*}/$QUEUE_SHARDS"; fi; '
            "start=$SECONDS && status=0 && workers=$(",
        )
    )
    assert "ci_cpu_quota) && python -c " in command
    assert '-n "$workers"' in command
    assert "-n auto" not in command
    assert "tests/" in command
    assert "-m unit" not in command


def test_workflow_worker_count_stays_within_this_container():
    prefix = _pytest_command().strip().removeprefix("run: ").split(" && python -c ")[0]
    result = subprocess.run(
        ["bash", "-e", "-c", f'{prefix} && echo "$workers"'], cwd=_ROOT, capture_output=True, text=True, check=True
    )
    workers = int(result.stdout)
    quota = re.search(r"cpu quota (\S+),", result.stderr).group(1)
    assert 1 <= workers <= len(os.sched_getaffinity(0))
    assert quota == "None" or workers <= max(1, int(float(quota)))


def test_workflow_keeps_xdist_groups_on_one_worker():
    assert "--dist loadgroup" in _pytest_command()


def test_unit_shards_measure_no_coverage():
    command = _pytest_command()
    assert "--cov" not in command
    assert "matrix.cov" not in command
    include = _workflow()["jobs"]["unit"]["strategy"]["matrix"].get("include", [])
    assert include in (
        [],
        "${{ fromJSON(github.event_name == 'merge_group' && format('[{0}]', needs.split.outputs.queue_312) || '[]') }}",
    )
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
    assert step["env"]["SEGMENT_DOWNLOAD_TIMEOUT_MINS"] == "1"


def _tests_workflow_steps() -> list[dict]:
    workflow = yaml.safe_load((_ROOT / ".github/workflows/test.yml").read_text())
    steps = [step for job in workflow["jobs"].values() for step in job.get("steps", [])]
    actions = {
        step["uses"].split(".github/actions/")[1] for step in steps if ".github/actions/" in step.get("uses", "")
    }
    for action in actions:
        steps += yaml.safe_load((_ROOT / ".github/actions" / action / "action.yml").read_text())["runs"].get(
            "steps", []
        )
    return steps


def test_tests_workflow_cache_restores_bound_download_timeout():
    restores = [
        step
        for step in _tests_workflow_steps()
        if step.get("uses", "").startswith("actions/cache/restore") and not step.get("with", {}).get("lookup-only")
    ]
    assert len(restores) >= 19
    for step in restores:
        assert step.get("env", {}).get("SEGMENT_DOWNLOAD_TIMEOUT_MINS") == "1", step.get("name", step["with"]["path"])


def test_nats_server_download_retries_fit_inside_every_step_timeout():
    action = yaml.safe_load((_ROOT / ".github/actions/nats-server/action.yml").read_text())
    lines = [line for step in action["runs"]["steps"] for line in step.get("run", "").splitlines()]
    curl = next(line.split() for line in lines if line.strip().startswith("curl "))
    flags = {flag: curl[i + 1] for i, flag in enumerate(curl[:-1]) if flag.startswith("--")}
    attempts = int(flags["--retry"]) + 1
    restores = [step for step in action["runs"]["steps"] if step.get("uses", "").startswith("actions/cache/restore")]
    restore_seconds = sum(int(step["env"]["SEGMENT_DOWNLOAD_TIMEOUT_MINS"]) * 60 for step in restores)
    budget = restore_seconds + attempts * (int(flags["--max-time"]) + int(flags["--retry-delay"]))
    assert int(flags["--connect-timeout"]) < int(flags["--max-time"])
    steps = [
        step
        for pattern in ("*.yml", "*.yaml")
        for workflow in (_ROOT / ".github/workflows").glob(pattern)
        for job in (yaml.safe_load(workflow.read_text()).get("jobs") or {}).values()
        for step in job.get("steps", [])
        if step.get("uses", "").endswith("/actions/nats-server")
    ]
    assert steps
    for step in steps:
        assert "timeout-minutes" in step, step
        assert budget < int(step["timeout-minutes"]) * 60, step


def _nats_server_steps() -> dict[str, dict]:
    action = yaml.safe_load((_ROOT / ".github/actions/nats-server/action.yml").read_text())
    return {step["name"]: step for step in action["runs"]["steps"]}


def test_nats_server_archive_is_cached_on_its_pinned_release_and_checksum():
    steps = _nats_server_steps()
    names = list(steps)
    restore = steps["Restore the pinned nats server archive"]
    install = steps["Install the pinned nats server"]
    save = steps["Save the pinned nats server archive"]
    assert names.index("Resolve the pinned nats server") < names.index(restore["name"]) < names.index(install["name"])
    assert names.index(install["name"]) < names.index(save["name"])
    assert restore["uses"] == "actions/cache/restore@v4"
    assert restore["with"] == {
        "path": "${{ steps.pin.outputs.archive }}",
        "key": "${{ steps.pin.outputs.name }}-${{ steps.pin.outputs.sha }}",
    }
    assert install["env"]["CACHE_HIT"] == "${{ steps.cache.outputs.cache-hit }}"
    assert save["uses"] == "actions/cache/save@v4"
    assert save["if"] == "steps.cache.outputs.cache-hit != 'true' && github.ref == 'refs/heads/main'"
    assert save["with"] == {"path": restore["with"]["path"], "key": "${{ steps.cache.outputs.cache-primary-key }}"}


def _run_nats_install(tmp_path: Path, cache_hit: str, cached: str) -> list[str]:
    name = "nats-server-v0-linux-amd64"
    binary = tmp_path / "src" / name / "nats-server"
    binary.parent.mkdir(parents=True)
    binary.write_text("#!/bin/sh\necho nats-server: v0\n")
    binary.chmod(0o755)
    good = tmp_path / "good.tar.gz"
    with tarfile.open(good, "w:gz") as tar:
        tar.add(binary.parent, arcname=name)
    archive = tmp_path / "runner" / f"{name}.tar.gz"
    archive.parent.mkdir()
    if cached == "valid":
        archive.write_bytes(good.read_bytes())
    elif cached == "corrupt":
        archive.write_bytes(b"corrupt")
    calls = tmp_path / "curl.log"
    stub = tmp_path / "bin" / "curl"
    stub.parent.mkdir()
    stub.write_text(
        '#!/usr/bin/env bash\necho "$@" >> "$CURL_LOG"\n'
        'while [[ $# -gt 0 ]]; do [[ "$1" == -o ]] && cp "$GOOD" "$2"; shift; done\n'
    )
    stub.chmod(0o755)
    env = {
        **os.environ,
        "PATH": f"{stub.parent}:{os.environ['PATH']}",
        "CURL_LOG": str(calls),
        "GOOD": str(good),
        "RUNNER_TEMP": str(archive.parent),
        "GITHUB_PATH": str(tmp_path / "path"),
        "GITHUB_ENV": str(tmp_path / "env"),
        "CACHE_HIT": cache_hit,
        "NATS_SERVER_RELEASE": "v0",
        "NATS_SERVER_NAME": name,
        "NATS_SERVER_SHA256": hashlib.sha256(good.read_bytes()).hexdigest(),
        "NATS_SERVER_ARCHIVE": str(archive),
    }
    run = _nats_server_steps()["Install the pinned nats server"]["run"]
    subprocess.run(["bash", "-c", run], env=env, check=True, capture_output=True, text=True)
    assert (tmp_path / "env").read_text() == "NATS_SERVER_REQUIRED=1\n"
    return calls.read_text().splitlines() if calls.exists() else []


def test_nats_server_install_skips_the_download_on_a_verified_cache_hit(tmp_path):
    assert _run_nats_install(tmp_path, "true", "valid") == []


def test_nats_server_install_downloads_on_a_cache_miss(tmp_path):
    assert len(_run_nats_install(tmp_path, "", "none")) == 1


def test_nats_server_install_downloads_again_when_the_cached_archive_fails_its_checksum(tmp_path):
    assert len(_run_nats_install(tmp_path, "true", "corrupt")) == 1


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
    assert install["env"] == {"PYTHON_PATH": "${{ steps.python.outputs.python-path }}"}
    assert install["run"].strip().splitlines() == [
        'uv venv --python "$PYTHON_PATH" "$HOME/venv"',
        'uv pip install --offline --no-index --find-links /opt/wheelhouse --python "$HOME/venv/bin/python" --excludes .github/test-excludes.txt -e ".[dev,all]" && { echo "::notice title=Wheelhouse hit::The runner wheelhouse served the install offline"; exit 0; }',
        'echo "::notice title=Wheelhouse miss::The runner wheelhouse cannot serve the install; fetching from PyPI"',
        'bash .github/scripts/pypi-retry.sh "Install dependencies" -- uv pip install --python "$HOME/venv/bin/python" --excludes .github/test-excludes.txt -e ".[dev,all]"',
    ]


def test_unit_restores_one_venv_per_interpreter_and_dependency_files():
    _, cache = _unit_step_index(lambda s: s.get("id") == "venv")
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
    _, cache = _unit_step_index(lambda s: s.get("id") == "venv")
    hit = f"steps.{cache['id']}.outputs.cache-hit != 'true'"
    for predicate in (
        lambda s: s.get("uses", "").startswith("astral-sh/setup-uv"),
        lambda s: s.get("name") == "Install dependencies",
    ):
        _, step = _unit_step_index(predicate)
        assert step["if"] in (hit, f"env.UNIT_SOURCE == '' && {hit}")


def test_unit_tests_run_from_the_venv():
    path_index, path = _unit_step_index(lambda s: s.get("name") == "Use the test environment")
    run_index, _ = _unit_step_index(lambda s: s.get("name") == "Run tests")
    assert path["run"].strip() == 'echo "$HOME/venv/bin" >> "$GITHUB_PATH"'
    assert path_index < run_index


def test_only_the_first_shard_installs_the_pinned_codex_cli_and_requires_its_live_test():
    job = yaml.safe_load((_ROOT / ".github/workflows/test.yml").read_text())["jobs"]["unit"]
    release = job["env"]["CODEX_RELEASE"]
    assert re.fullmatch(r"rust-v\d+\.\d+\.\d+", release)
    only = "matrix.python-version == '3.12' && matrix.shard == 1"
    restore_index, restore = _unit_step_index(lambda s: s.get("id") == "codex")
    install_index, install = _unit_step_index(lambda s: s.get("name") == "Install the Codex CLI")
    use_index, use = _unit_step_index(lambda s: s.get("name") == "Use the Codex CLI")
    run_index, _ = _unit_step_index(lambda s: s.get("name") == "Run tests")
    assert restore["if"] in (only, f"needs.split.outputs.source == '' && {only}")
    assert restore["uses"] == "actions/cache/restore@v4"
    assert restore["with"] == {"path": "~/codex", "key": "codex-${{ runner.os }}-${{ env.CODEX_RELEASE }}"}
    install_if = "steps.codex.outcome == 'success' && steps.codex.outputs.cache-hit != 'true'"
    assert install["if"] in (install_if, f"needs.split.outputs.source == '' && {install_if}")
    assert install["run"].startswith("set -o pipefail\n")
    assert "releases/download/${CODEX_RELEASE}/codex-x86_64-unknown-linux-musl.tar.gz" in install["run"]
    use_if = "steps.codex.outcome == 'success'"
    assert use["if"] in (use_if, f"needs.split.outputs.source == '' && {use_if}")
    assert use["run"].strip().splitlines() == [
        'echo "$HOME/codex" >> "$GITHUB_PATH"',
        'echo "CODEX_CLI_REQUIRED=1" >> "$GITHUB_ENV"',
    ]
    assert restore_index < install_index < use_index < run_index


def test_main_seeds_the_codex_cache_that_unit_shards_restore():
    workflows = _ROOT / ".github/workflows"
    jobs = yaml.safe_load((workflows / "test.yml").read_text())["jobs"]
    seed_workflow = yaml.safe_load((workflows / "codex-cache.yml").read_text())
    triggers = seed_workflow[True]
    assert "schedule" in triggers
    assert triggers["push"]["branches"] == ["main"]
    seed = seed_workflow["jobs"]["seed"]
    assert (
        seed["env"]["CODEX_RELEASE"]
        == jobs["unit"]["env"]["CODEX_RELEASE"]
        == jobs["unit-selected"]["env"]["CODEX_RELEASE"]
    )
    assert "'anton-oss'" in seed["runs-on"]
    restore = next(step for step in seed["steps"] if step.get("id") == "codex")
    assert restore["uses"] == "actions/cache@v4"
    assert restore["with"] == {"path": "~/codex", "key": "codex-${{ runner.os }}-${{ env.CODEX_RELEASE }}"}
    install = next(step for step in seed["steps"] if step.get("name") == "Install the Codex CLI")
    assert restore["if"] == "runner.environment == 'self-hosted'"
    assert install["if"] == "steps.codex.outcome == 'success' && steps.codex.outputs.cache-hit != 'true'"
    assert "releases/download/${CODEX_RELEASE}/codex-x86_64-unknown-linux-musl.tar.gz" in install["run"]


def test_main_seeds_the_nats_server_cache_that_unit_shards_restore():
    steps = yaml.safe_load((_ROOT / ".github/workflows/codex-cache.yml").read_text())["jobs"]["seed"]["steps"]
    names = [step.get("name") for step in steps]
    take = steps[names.index("Take the nats server action")]
    seed = steps[names.index("Seed the pinned nats server")]
    assert take["uses"] == "actions/checkout@v4"
    assert take["with"] == {"sparse-checkout": ".github/actions/nats-server"}
    assert seed["uses"] == "./.github/actions/nats-server"
    assert take["if"] == seed["if"] == "runner.environment == 'self-hosted'"
    assert steps.index(take) < steps.index(seed)


def test_unit_install_keeps_playwright_and_excludes_the_grpc_exporter():
    excludes = (_ROOT / ".github/test-excludes.txt").read_text().split()
    assert excludes == ["opentelemetry-exporter-otlp-proto-grpc"]


def test_queue_environment_matches_the_baked_browser_and_invalidates_old_venvs():
    workflow = _workflow()
    assert (_ROOT / ".github/test-constraints.txt").read_text().strip() == "playwright==1.63.0"
    for job in ("test-count", "split", "unit"):
        assert workflow["jobs"][job]["env"]["UV_CONSTRAINT"] == "${{ github.workspace }}/.github/test-constraints.txt"
        cache = next(step for step in workflow["jobs"][job]["steps"] if step.get("id") == "venv")
        assert "${{ hashFiles('.github/test-constraints.txt') }}" in cache["with"]["key"]


def test_every_job_sharing_the_test_environment_installs_the_baked_browser_version():
    sharing, unpinned = [], []
    for path in sorted((_ROOT / ".github/workflows").glob("*.yml")):
        for name, job in (yaml.safe_load(path.read_text())["jobs"] or {}).items():
            keys = [str((step.get("with") or {}).get("key", "")) for step in job.get("steps", [])]
            if any(key.startswith("venv-") for key in keys):
                sharing.append(f"{path.name}:{name}")
                if (job.get("env") or {}).get(
                    "UV_CONSTRAINT"
                ) != "${{ github.workspace }}/.github/test-constraints.txt":
                    unpinned.append(f"{path.name}:{name}")
    assert "count-record.yml:count-record" in sharing
    assert unpinned == []


def test_queue_browser_setup_cannot_enter_the_downloading_action():
    steps = _workflow()["jobs"]["unit"]["steps"]
    install = next(step for step in steps if step.get("uses") == "./.github/actions/browser-cache")
    assert "github.event_name != 'merge_group'" in install["if"]
    offline = next(step for step in steps if "use-image.sh" in step.get("run", ""))
    assert (
        offline["if"]
        == "needs.split.outputs.source == '' && matrix.python-version == '3.12' && github.event_name == 'merge_group'"
    )
    assert steps.index(offline) < steps.index(next(step for step in steps if step.get("name") == "Run tests"))
    assert offline["env"]["PLAYWRIGHT_BROWSERS_PATH"] == "${{ github.workspace }}/.playwright"


def test_only_a_dev_push_saves_the_test_environment_after_installing_it():
    jobs = yaml.safe_load((_ROOT / ".github/workflows/test.yml").read_text())["jobs"]
    callers = {
        name: job["steps"] for name, job in jobs.items() if any(s.get("id") == "venv" for s in job.get("steps", []))
    }
    recorder_path = _ROOT / ".github/workflows/count-record.yml"
    if recorder_path.exists():
        recorder = yaml.safe_load(recorder_path.read_text())["jobs"]["count-record"]
        assert "count-record" not in jobs
        jobs["count-record"] = recorder
        callers["count-record"] = recorder["steps"]
    assert sorted(callers) == ["count-record", "split", "test-count", "unit", "unit-rerun", "unit-selected"]
    for name, steps in callers.items():
        restore = next(step for step in steps if step.get("id") == "venv")
        assert restore["uses"] == "actions/cache/restore@v4"
        saves = [
            step for step in steps if step.get("uses") == "actions/cache/save@v4" and step["with"]["path"] == "~/venv"
        ]
        guards = {"unit-selected": "github.event_name == 'pull_request'", "test-count": "github.event_name != 'push'"}
        if name in guards:
            assert guards[name] in jobs[name]["if"]
            assert saves == []
            continue
        [save] = saves
        install = next(step for step in steps if step.get("name") == "Install dependencies")
        assert save["name"] == "Save the test environment from dev"
        assert save["with"] == {"path": restore["with"]["path"], "key": "${{ steps.venv.outputs.cache-primary-key }}"}
        assert save["if"] == (
            "env.UNIT_SOURCE == '' && steps.venv.outputs.cache-hit != 'true'"
            " && github.event_name == 'push' && github.ref == 'refs/heads/dev'"
        )
        assert steps.index(restore) < steps.index(install) < steps.index(save)
    assert not any(
        step.get("uses") == "actions/cache@v4" and "venv" in step.get("with", {}).get("path", "")
        for job in jobs.values()
        for step in job.get("steps", [])
    )


def test_unit_matrix_runs_one_shard_per_split():
    command = _pytest_command()
    assert '--shard "$SHARD"' in command
    _, step = _unit_step_index(lambda s: s.get("name") == "Run tests")
    split = r"^\$\{\{ matrix\.shard \}\}/\$\{\{ matrix\.python-version == '3\.12' && (\d+) \|\| (\d+) \}\}$"
    coverage_shards, plain_shards = (int(count) for count in re.search(split, step["env"]["SHARD"]).groups())
    workflow = yaml.safe_load((_ROOT / ".github/workflows/test.yml").read_text())
    matrix = workflow["jobs"]["unit"]["strategy"]["matrix"]
    excluded = {(entry["python-version"], entry["shard"]) for entry in matrix.get("exclude", [])}
    assert matrix["python-version"] == ["3.12"]
    assert [shard for shard in matrix["shard"] if ("3.12", shard) not in excluded] == list(
        range(1, coverage_shards + 1)
    )
    assert coverage_shards >= plain_shards > 1
    merge = next(step for step in workflow["jobs"]["sonar"]["steps"] if step.get("name") == "Merge shard coverage")
    assert 'combine.sh --downloaded "$SHARDS" ' in merge["run"]
    assert merge["env"]["SHARDS"] == (
        f"${{{{ needs.select.outputs.scope == 'selected' && needs.select.outputs.count || {coverage_shards} }}}}"
    )
    assert "--splits" not in command


def test_a_hung_unit_shard_fails_near_twice_the_slowest_shard():
    slowest_shard_minutes = 336 / 60
    unit = _workflow()["jobs"]["unit"]
    steps = {step.get("name"): step for step in unit["steps"]}
    timeout = steps["Run tests"]["timeout-minutes"]
    assert 2 * slowest_shard_minutes <= timeout <= 3 * slowest_shard_minutes
    browser = steps["Install the browser that page tests drive"]["timeout-minutes"]
    assert unit["timeout-minutes"] >= browser + timeout + 2


def test_a_hung_unit_test_dumps_every_thread_stack_within_a_minute():
    command = _pytest_command()
    slowest_test = _slowest_committed_test()
    timeout = int(re.search(r"-o faulthandler_timeout=(\d+)", command).group(1))
    assert slowest_test < timeout <= 60
    assert _slowest_committed_test(skip_rearmed=False) < int(re.search(r"--timeout=(\d+)", command).group(1))
    assert "no:faulthandler" not in command
    assert "faulthandler_exit_on_timeout" not in command


def test_the_hung_test_bound_reads_the_committed_durations_not_the_downloaded_copy(tmp_path, monkeypatch):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    for name in (".test_durations", ".test_durations-3.12"):
        (tmp_path / name).write_text(json.dumps({"tests/test_a.py::test_a": 1.0}))
    subprocess.run(["git", "-C", str(tmp_path), "add", "."], check=True)
    subprocess.run(
        ["git", "-C", str(tmp_path), "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "d"], check=True
    )
    for name in (".test_durations", ".test_durations-3.12"):
        (tmp_path / name).write_text(json.dumps({"tests/test_a.py::test_a": 90.0}))
    monkeypatch.setattr(sys.modules[__name__], "_ROOT", tmp_path)
    assert _slowest_committed_test() == 1.0


def test_the_hung_test_bound_skips_a_file_that_rearms_its_own_dump(tmp_path, monkeypatch):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests/test_a.py").write_text("def test_a():\n    pass\n")
    (tmp_path / "tests/test_slow.py").write_text(
        "@pytest.fixture(autouse=True)\ndef rearm():\n    faulthandler.dump_traceback_later(250)\n"
    )
    durations = {"tests/test_a.py::test_a": 1.0, "tests/test_slow.py::test_slow": 90.0}
    for name in (".test_durations", ".test_durations-3.12"):
        (tmp_path / name).write_text(json.dumps(durations))
    subprocess.run(["git", "-C", str(tmp_path), "add", "."], check=True)
    subprocess.run(
        ["git", "-C", str(tmp_path), "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "d"], check=True
    )
    monkeypatch.setattr(sys.modules[__name__], "_ROOT", tmp_path)
    assert _slowest_committed_test() == 1.0
    (tmp_path / "tests/test_slow.py").write_text("def test_slow():\n    pass\n")
    assert _slowest_committed_test() == 90.0


def test_tests_keep_dev_pull_request_and_integration_triggers():
    triggers = yaml.safe_load((_ROOT / ".github/workflows/test.yml").read_text())[True]
    assert set(triggers["pull_request"]["branches"]) in ({"dev", "main"}, {"dev"})
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
        "${{ github.event_name == 'pull_request' && github.base_ref == 'dev' && 'origin/dev'"
        " || github.event.pull_request.base.sha || github.event.merge_group.base_sha"
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
    assert "Select mutation tests before browser setup" not in names
    install = names.index("Install the browser that page tests drive")
    assert steps[install]["if"] == "needs.mutation-plan.outputs.browser == 'true'"
    assert steps[install]["id"] == "browser"
    assert steps[install]["timeout-minutes"] == 6
    job = _mutation_workflow()["jobs"]["mutation"]
    assert job["env"]["PLAYWRIGHT_BROWSERS_PATH"] == "${{ github.workspace }}/.playwright"
    selection = next(step for step in steps if step.get("name") == "Download planned mutation test selection")
    assert selection["with"]["name"] == "mutation-selection"
    failure = next(step for step in steps if step.get("name") == "Report browser setup failure")
    assert failure["if"] == "failure() && steps.browser.outcome == 'failure'"
    assert "::error::" in failure["run"]
    assert "six minute" in failure["run"]
    assert "exit 1" in failure["run"]


def test_mutation_browser_dependencies_use_the_responsive_mirror():
    steps = _mutation_workflow()["jobs"]["mutation"]["steps"]
    setup = _browser_setup_steps(steps)
    mirror = next(step for step in setup if step.get("name") == "Use the Ubuntu archive for browser dependencies")
    install = _browser_install(setup)
    assert setup.index(mirror) < setup.index(install)
    assert mirror["if"] in (
        "steps.selection.outputs.browser == 'true'",
        "steps.launch.outputs.ready != 'true'",
        "steps.launch.outputs.ready != 'true' && runner.environment == 'github-hosted'",
    )
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
        assert _browser_install(steps)["timeout-minutes"] == 6


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
        assert install["if"] == "${{ !cancelled() && steps.artifacts.outputs.browser == 'true' }}"


def test_equivalence_runs_storage_then_page_replays_each_group_at_once():
    steps = _equivalence_steps()
    script = _EQUIVALENCE.read_text()
    store, pages = _equivalence_group("store"), _equivalence_group("page")
    assert store.count("repository_replay.py") == 3 and "page_replay.py" not in store
    assert pages.count("page_replay.py") == 3 and "repository_replay.py" not in pages
    for run in (store, pages):
        assert run.count(" &\n") == 3
        assert run.index(" &\n") < run.index("settle") < run.index("cmp ")
    assert "|| failed=1" in script and 'return "$failed"' in script
    assert 'cat "$RUNNER_TEMP/$name.log"' in script and "return 1" in script
    [store_step] = [step for step in steps if step.get("run", "").endswith(" store")]
    [page_step] = [step for step in steps if step.get("run", "").endswith(" page")]
    assert steps.index(store_step) < steps.index(_browser_install(steps)) < steps.index(page_step)
    assert "Path('head/" not in script


def test_equivalence_plants_each_fault_in_a_file_that_exists():
    script = _equivalence_group("plant").split("<<'PY'\n", 1)[1].rsplit("PY", 1)[0]
    loop = next(node for node in ast.walk(ast.parse(script)) if isinstance(node, ast.For))
    planted = [(ast.literal_eval(entry.elts[0].args[0]), ast.literal_eval(entry.elts[1])) for entry in loop.iter.elts]
    assert [path.split("/", 1)[0] for path, _ in planted] == ["fault-store", "fault-chat"]
    for path, needle in planted:
        assert (_ROOT / path.split("/", 1)[1]).read_text().count(needle) == 1


def test_equivalence_grades_with_the_base_copy():
    steps = _equivalence_steps()
    [base] = [step for step in steps if step.get("with", {}).get("path") == "base"]
    assert base["with"]["ref"] == "${{ github.event.pull_request.base.sha }}"
    [pick] = [step for step in steps if step.get("name") == "Pick the protected grader"]
    assert pick["run"].startswith("if [[ -f base/tests/swarm_ledger/equivalence.sh ]]; then\n")
    assert pick["run"].index('"GRADER=$PWD/base"') < pick["run"].index("else") < pick["run"].index('"GRADER=$PWD/head"')
    assert pick["run"].index("else") < pick["run"].index("::warning::")
    graded = [step for step in steps if "equivalence.sh" in step.get("run", "") and step is not pick]
    assert [step["run"] for step in graded] == [
        f'bash "$GRADER/tests/swarm_ledger/equivalence.sh" {group}' for group in ("plant", "store", "page")
    ]
    assert all(steps.index(base) < steps.index(pick) < steps.index(step) for step in graded)
    assert _browser_install(steps)["uses"] == "./base/.github/actions/browser-cache"
    for step in steps:
        run = step.get("run", "")
        assert "cmp " not in run and "_replay.py" not in run and "head/tests" not in run
        assert step is pick or "GITHUB_ENV" not in run
    script = _EQUIVALENCE.read_text()
    assert 'replays=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)' in script and "head/tests" not in script


def _equivalence_steps() -> list[dict]:
    return yaml.safe_load((_ROOT / ".github/workflows/equivalence.yml").read_text())["jobs"]["ledger-equivalence"][
        "steps"
    ]


def _equivalence_group(name: str) -> str:
    return _EQUIVALENCE.read_text().split(f"\n{name}() {{\n", 1)[1].split("\n}\n", 1)[0]


def test_equivalence_runs_on_every_ledger_pull_request_without_a_body_marker():
    workflow = yaml.safe_load((_ROOT / ".github/workflows/equivalence.yml").read_text())
    job = workflow["jobs"]["ledger-equivalence"]
    assert "if" not in job
    assert all("pull_request.body" not in str(step.get("if", "")) for step in job["steps"])
    [head] = [step for step in job["steps"] if step.get("with", {}).get("path") == "head"]
    assert "ref" not in head["with"]
    assert "paths" not in workflow[True]["pull_request"]


def test_mutation_gate_plants_itself_whenever_its_code_changes():
    job = _workflow()["jobs"]["mutation-self-plant"]
    assert job["if"] == "${{ !cancelled() && github.event_name == 'pull_request' }}"
    assert job["strategy"]["matrix"]["mode"] == ["kill", "survive"]
    steps = job["steps"]
    [base] = [step for step in steps if step.get("with", {}).get("path") == "base"]
    assert base["with"]["ref"] == "${{ github.event.pull_request.base.sha }}"
    [changed] = [step for step in steps if step.get("id") == "changed"]
    listed = changed["run"].split("for path in ", 1)[1].split("; do", 1)[0].replace("\\\n", " ").split()
    assert {"scripts/ci_mutation", "tests/mutation_self_plant", "hooks/context/conditions.py", "pyproject.toml"} <= set(
        listed
    )
    assert '! diff -rq "base/$path" "head/$path"' in changed["run"]
    [pick] = [step for step in steps if step.get("name") == "Pick the protected grader"]
    assert pick["run"].startswith("if [[ -f base/tests/mutation_self_plant/plant.py ]]; then\n")
    [grade] = [step for step in steps if '"$GRADER"' in step.get("run", "")]
    assert grade["working-directory"] == "head"
    assert steps.index(base) < steps.index(changed) < steps.index(pick) < steps.index(grade)
    assert all("steps.changed.outputs.plant == 'true'" in str(step["if"]) for step in steps[steps.index(changed) + 1 :])


@pytest.mark.parametrize("doc", ["README.md", "index.md"])
def test_workflow_badges_point_at_existing_workflows(doc):
    names = re.findall(r"actions/workflows/([\w.-]+\.yml)", (_ROOT / doc).read_text())
    assert names
    assert all((_ROOT / ".github/workflows" / name).is_file() for name in names), names


def _workflow() -> dict:
    return yaml.safe_load((_ROOT / ".github/workflows/test.yml").read_text())


def test_shards_wait_only_on_the_durations_lookup():
    jobs = _workflow()["jobs"]
    assert "already-tested" not in jobs
    assert jobs["split"]["needs"] in (
        ["durations", "select", "reuse", "admission"],
        ["select", "reuse", "admission"],
        ["test-count", "select", "reuse", "admission"],
    )
    assert jobs["unit"]["needs"] == ["split", "select", "reuse", "admission"]
    assert jobs["lint"]["needs"] in (["reuse"], ["reuse", "admission"])


def test_full_suite_collection_starts_beside_selection_and_feeds_split():
    jobs = _workflow()["jobs"]
    assert "collect" not in jobs
    collect = jobs["test-count"]
    assert collect["needs"] == jobs["select"]["needs"]
    assert "needs.select" not in collect["if"]
    assert collect["strategy"]["matrix"] == {"python-version": ["3.12"]}
    assert jobs["split"]["strategy"]["matrix"] == collect["strategy"]["matrix"]
    command = next(
        step["run"] for step in collect["steps"] if step.get("name") == "Hold the base revision's test count"
    )
    assert "tests.duration_coverage import collected_tests" in command
    assert "collected_tests(Path.cwd())" in command
    upload = next(step for step in collect["steps"] if step.get("name") == "Upload the early collection")
    download = next(step for step in jobs["split"]["steps"] if step.get("name") == "Download the early collection")
    assert upload["with"]["if-no-files-found"] == "error"
    assert download["with"] == {
        "name": upload["with"]["name"],
        "client-id": "${{ secrets.TCC_CI_CLIENT_ID }}",
        "private-key": "${{ secrets.TCC_CI_APP_PRIVATE_KEY }}",
    }
    assert "test-count" not in jobs["split"]["needs"]
    adopt = next(step for step in jobs["split"]["steps"] if step.get("name") == "Adopt latest dev durations")
    assert "--collection collected.json" in adopt["run"]
    assert "test-count" in jobs["gate-required"]["needs"]
    assert "split" in jobs["gate-required"]["needs"]


@pytest.mark.parametrize("result", ["success", "failure", "skipped", "cancelled"])
def test_split_rejects_a_failed_collection_before_reading_any_artifact(tmp_path, result):
    steps = _workflow()["jobs"]["split"]["steps"]
    guard = next(step for step in steps if step.get("name") == "Require successful early collection")
    download = next(step for step in steps if step.get("name") == "Download the early collection")
    assert guard["if"] == download["if"]
    assert guard["env"]["COLLECTION_JOB"] == "test-count (${{ matrix.python-version }})"
    assert guard["env"]["GH_TOKEN"] == "${{ steps.app-token.outputs.token }}"
    mint = next(step for step in steps if step.get("id") == "app-token")
    assert steps.index(mint) < steps.index(guard)
    assert steps.index(guard) < steps.index(download)
    binary = tmp_path / "bin"
    binary.mkdir()
    (binary / "gh").write_text(
        '#!/usr/bin/env bash\nprintf x >> "$CALLS"\n'
        'if [[ "$(wc -c < "$CALLS")" -lt 2 ]]; then echo "in_progress "; else echo "completed $RESULT"; fi\n'
    )
    (binary / "sleep").write_text("#!/usr/bin/env bash\n")
    for stub in binary.iterdir():
        stub.chmod(0o755)
    outcome = subprocess.run(
        ["bash", "-e", "-c", guard["run"]],
        env=dict(
            os.environ,
            PATH=f"{binary}:{os.environ['PATH']}",
            CALLS=str(tmp_path / "calls"),
            RESULT=result,
            GH_TOKEN="app",
            COLLECTION_JOB="test-count (3.12)",
        ),
        capture_output=True,
        text=True,
    )
    assert (outcome.returncode == 0) == (result == "success")
    assert (tmp_path / "calls").read_text() == "xx"


def _await_steps():
    return yaml.safe_load((_ROOT / ".github/actions/await-download/action.yml").read_text())["runs"]["steps"]


def test_a_fork_split_waits_for_the_collection_instead_of_polling_without_a_token():
    jobs = _workflow()["jobs"]
    steps = jobs["split"]["steps"]
    guard = next(step for step in steps if step.get("name") == "Require successful early collection")
    download = next(step for step in steps if step.get("name") == "Download the early collection")
    fork = next(step for step in steps if step.get("name") == "Wait for the early collection on a fork")
    upload = next(step for step in jobs["test-count"]["steps"] if step.get("name") == "Upload the early collection")
    mint = next(step for step in steps if step.get("id") == "app-token")
    same = " && (github.event_name != 'pull_request' || github.event.pull_request.head.repo.full_name == github.repository)"
    assert guard["if"] == mint["if"] and guard["if"].endswith(same)
    assert fork["if"] == guard["if"].replace(
        same,
        " && github.event_name == 'pull_request' && github.event.pull_request.head.repo.full_name != github.repository",
    )
    assert fork["uses"] == "./.github/actions/await-download"
    assert fork["with"] == {"name": upload["with"]["name"]}
    assert (
        steps.index(download)
        < steps.index(fork)
        < steps.index(next(step for step in steps if step.get("name") == "Adopt latest dev durations"))
    )


def test_the_collection_wait_outlasts_the_count_job_timeout(tmp_path):
    attempts = [step for step in _await_steps() if step.get("uses")]
    waits = [step for step in _await_steps() if step.get("name", "").startswith("Wait ")]
    assert len(waits) == len(attempts) - 1
    assert all(step["uses"] == "actions/download-artifact@v4" for step in attempts)
    assert all(step["continue-on-error"] is True for step in attempts)
    assert all(step["with"] == {"name": "${{ inputs.name }}", "path": "${{ github.workspace }}"} for step in attempts)
    binary = tmp_path / "bin"
    binary.mkdir()
    (binary / "sleep").write_text('#!/usr/bin/env bash\necho "$1" >> "$SLEPT"\n')
    (binary / "sleep").chmod(0o755)
    for wait in waits:
        subprocess.run(
            ["bash", "-e", "-c", wait["run"]],
            env=dict(os.environ, PATH=f"{binary}:{os.environ['PATH']}", SLEPT=str(tmp_path / "slept")),
            check=True,
        )
    slept = sum(int(line) for line in (tmp_path / "slept").read_text().split())
    jobs = _workflow()["jobs"]
    assert jobs["test-count"]["timeout-minutes"] * 60 <= slept <= (jobs["split"]["timeout-minutes"] - 5) * 60


@pytest.mark.parametrize("ready", [0, 25, 400, None])
def test_the_collection_wait_downloads_once_the_artifact_lands_and_stays_red_when_it_never_does(tmp_path, ready):
    outcomes, clock, tried = {}, 0, []
    binary = tmp_path / "bin"
    binary.mkdir()
    (binary / "sleep").write_text('#!/usr/bin/env bash\necho "$1" > "$SLEPT"\n')
    (binary / "sleep").chmod(0o755)
    env = dict(os.environ, PATH=f"{binary}:{os.environ['PATH']}", SLEPT=str(tmp_path / "slept"), ARTIFACT="x")
    final = None
    for step in _await_steps():
        match = re.fullmatch(r"steps\.(\w+)\.outcome == 'failure'", step.get("if", "steps.none.outcome == 'failure'"))
        assert match, step["if"]
        if "if" in step and outcomes.get(match.group(1)) != "failure":
            continue
        if step.get("uses"):
            tried.append(clock)
            outcomes[step["id"]] = "success" if ready is not None and clock >= ready else "failure"
            continue
        done = subprocess.run(["bash", "-e", "-c", step["run"]], env=env, capture_output=True, text=True)
        if step.get("name", "").startswith("Wait "):
            clock += int((tmp_path / "slept").read_text())
        else:
            final = done
    if ready is None:
        assert len(tried) == len([step for step in _await_steps() if step.get("uses")])
        assert final.returncode == 1
        assert "::error" in final.stdout
    else:
        assert final is None
        assert tried[-1] >= ready
        assert len(tried) == 1 or tried[-2] < ready


@pytest.mark.parametrize("collection,floor", [(0, 0), (1, 0), (0, 1), (1, 1)])
def test_collection_and_count_floor_both_have_to_succeed(tmp_path, collection, floor):
    base = tmp_path / "base" / "tests"
    base.mkdir(parents=True)
    (base / "count_floor.py").touch()
    commands = tmp_path / "commands"
    binary = tmp_path / "bin"
    binary.mkdir()
    python = binary / "python"
    python.write_text(
        '#!/usr/bin/env bash\nset -euo pipefail\nprintf "%s\\n" "$1" >> "$CALLS"\n'
        'if [[ "$1" == -c ]]; then exit "$COLLECTION_EXIT"; fi\n'
        'if [[ -n "${TEST_COUNT_HEAD_COLLECTION:-}" ]]; then\n'
        '  [[ "$COLLECTION_EXIT" == 0 ]] || exit "$COLLECTION_EXIT"\n'
        '  printf "[]" > "$TEST_COUNT_HEAD_COLLECTION"\n'
        'fi\nexit "$FLOOR_EXIT"\n'
    )
    python.chmod(0o755)
    step = next(
        step
        for step in _workflow()["jobs"]["test-count"]["steps"]
        if step.get("name") == "Hold the base revision's test count"
    )
    outcome = subprocess.run(
        ["bash", "-e", "-c", step["run"]],
        env=dict(
            os.environ,
            RUNNER_TEMP=str(tmp_path),
            GITHUB_WORKSPACE=str(tmp_path),
            PATH=f"{binary}:{os.environ['PATH']}",
            CALLS=str(commands),
            COLLECTION_EXIT=str(collection),
            FLOOR_EXIT=str(floor),
            TEST_COUNT_HEAD_COLLECTION=(
                str(tmp_path / "collected.json") if "TEST_COUNT_HEAD_COLLECTION" in step.get("env", {}) else ""
            ),
        ),
        capture_output=True,
        text=True,
    )
    assert (outcome.returncode == 0) == (collection == floor == 0)
    expected = ["-m"] if "TEST_COUNT_HEAD_COLLECTION" in step.get("env", {}) else ["-c", "-m"]
    assert sorted(commands.read_text().splitlines()) == expected


def test_promotion_shard_reports_use_the_pushed_grader_and_prs_keep_the_base():
    steps = _workflow()["jobs"]["shard-check"]["steps"]
    checkout = next(step for step in steps if step.get("uses", "").startswith("actions/checkout@"))
    ref = checkout["with"]["ref"]
    assert "github.event_name == 'push' && github.sha" in ref
    assert "github.event_name == 'pull_request' && github.base_ref" in ref
    assert ref.index("github.event_name == 'push' && github.sha") < ref.index("github.event.before")


def test_shard_verification_starts_from_the_plan_and_prefetches_complete_reports():
    jobs = _workflow()["jobs"]
    job = jobs["shard-check"]
    assert set(job["needs"]) == {"split", "select", "reuse", "admission"}
    stream = next(step for step in job["steps"] if "python -m scripts.ci_gate_shards" in step.get("run", ""))
    assert stream["env"]["GH_TOKEN"] == "${{ steps.app-token.outputs.token }}"
    mint = next(step for step in job["steps"] if step.get("id") == "app-token")
    assert "if" not in mint
    assert mint["with"]["permission-actions"] == "read"
    assert (
        stream["env"]["QUEUE_PLAN"]
        == "${{ github.event_name == 'merge_group' && format('[{0}]', needs.split.outputs.queue_312) || '' }}"
    )
    assert f"seq 1 {len(jobs['unit']['strategy']['matrix']['shard'])}" in stream["run"]
    assert "jq -ce '[.[].shard]'" in stream["run"]
    check = next(
        step["run"]
        for step in job["steps"]
        if step.get("name") == "Check every collected test ran in exactly one shard"
    )
    assert (
        "tests.shard_check shard-reports/durations-*/durations.json --collected collected-data/collected.json" in check
    )
    assert "REUSED" in check
    assert job["timeout-minutes"] == "${{ github.event_name == 'merge_group' && 42 || 60 }}"
    assert job["permissions"] == {"contents": "read"}


def test_queue_gate_is_the_early_verifier_and_the_standard_gate_summarizes_queue_runs():
    workflow = _workflow()
    jobs = workflow["jobs"]
    summary = jobs["gate-required"]
    assert summary["name"] == ci_gate_queue.SUMMARY_NAME
    assert jobs["queue-fail-fast"] == ci_gate_queue.watcher(summary["runs-on"])
    normalized = ci_gate_queue.normalize({"test.yml": workflow})["test.yml"]["jobs"]
    assert normalized["gate-required"]["name"] == "Gate — Required"
    assert normalized["queue-fail-fast"]["name"] == "queue-fail-fast"
    labels = [job.get("name", key) for key, job in jobs.items()]
    assert sum("Gate — Required" in label for label in labels) == 2


def test_unit_shards_check_out_full_history_without_old_file_contents():
    steps = _workflow()["jobs"]["unit"]["steps"]
    checkouts = [s for s in steps if s.get("uses", "").startswith("actions/checkout")]
    assert checkouts
    for checkout in checkouts:
        assert {key: value for key, value in checkout["with"].items() if key != "ref"} == {
            "fetch-depth": 0,
            "filter": "blob:none",
        }
        assert checkout["with"].get("ref") in (None, "${{ github.sha }}")


def test_a_stalled_unit_checkout_aborts_within_a_minute_and_retries_once_in_place():
    steps = _workflow()["jobs"]["unit"]["steps"]
    first, second = [s for s in steps if s.get("uses", "").startswith("actions/checkout")]
    assert steps.index(first) == 0 and steps.index(second) == 1
    for checkout in (first, second):
        assert checkout["uses"] == "actions/checkout@v4"
        assert checkout["env"] == {"GIT_HTTP_LOW_SPEED_LIMIT": "1000", "GIT_HTTP_LOW_SPEED_TIME": "60"}
        assert checkout["with"] == {"ref": "${{ github.sha }}", "fetch-depth": 0, "filter": "blob:none"}
    assert first["continue-on-error"] is True
    assert "if" not in first
    assert "continue-on-error" not in second
    assert second["if"] == f"steps.{first['id']}.outcome == 'failure'"


def test_unit_pins_an_exact_uv_version():
    _, uv = _unit_step_index(lambda s: s.get("uses", "").startswith("astral-sh/setup-uv"))
    assert re.fullmatch(r"\d+\.\d+\.\d+", uv["with"]["version"])


def _collected_suite() -> tuple[list[str], str]:
    split = _ROOT / "collected.json"
    if split.exists():
        text = split.read_text()
        return json.loads(text), text
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "tests/", "--collect-only", "-q", "-n", "0", "-o", "addopts="],
        cwd=_ROOT,
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, "PYTEST_ADDOPTS": "", "PYTEST_DISABLE_PLUGIN_AUTOLOAD": ""},
    )
    assert result.returncode == 0, result.stdout + result.stderr
    return [line for line in result.stdout.splitlines() if line.startswith("tests/") and "::" in line], result.stdout


def _check_stored_durations(nodeids: list[str] | None = None):
    partial = nodeids is not None and not (_ROOT / "collected.json").exists()
    collected, output = (nodeids, "\n".join(nodeids)) if partial else _collected_suite()
    assert collected, output
    stored = json.loads((_ROOT / ".test_durations").read_text())
    # The queue split drops the tests its run reuses from the durations it hands the shards.
    reused = json.loads((_ROOT / "reused.json").read_text()) if (_ROOT / "reused.json").exists() else {}
    tests, files = set(reused.get("tests", ())), set(reused.get("files", ()))
    judged = [
        nodeid
        for nodeid in collected
        if nodeid.split("::", 1)[0] not in files and re.sub(r"@[^\[\]]*$", "", nodeid) not in tests
    ]
    missing = [nodeid for nodeid in judged if re.sub(r"@[^\[\]]*$", "", nodeid) not in stored]
    # A partial run holds every test its change adds, so its missing share is judged against the stored suite.
    suite = max(len(judged), len(stored)) if partial else len(judged)
    assert len(missing) * 10 <= suite, (
        f"{len(missing)} of {suite} tests have no stored duration; refresh them with: python -m tests.refresh_durations"
    )


def test_stored_durations_cover_the_collected_suite(request):
    _check_stored_durations(
        None if request.config.getoption("shard") else [item.nodeid for item in request.session.items]
    )


def test_unit_shards_read_the_suite_the_split_job_collected():
    jobs = _workflow()["jobs"]
    upload = next(step for step in jobs["split"]["steps"] if step.get("name") == "Upload collected tests")
    _, download = _unit_step_index(lambda s: s.get("name") == "Download the suite the split job collected")
    assert download["uses"] == "./.github/actions/retry-download"
    assert download["with"] == {
        "name": upload["with"]["name"],
        "client-id": "${{ secrets.TCC_CI_CLIENT_ID }}",
        "private-key": "${{ secrets.TCC_CI_APP_PRIVATE_KEY }}",
    }
    assert "if" not in upload
    assert download["if"] == "needs.split.outputs.source == ''"


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
        _check_stored_durations()
    else:
        with pytest.raises(AssertionError, match=f"{missing} of 100 tests"):
            _check_stored_durations()


@pytest.mark.parametrize("selected", [False, True])
@pytest.mark.parametrize("missing", [10, 11])
def test_stored_durations_read_the_split_jobs_collected_suite(tmp_path, monkeypatch, missing, selected):
    nodeids = [f"tests/test_probe.py::test_{i}" for i in range(100)]
    (tmp_path / ".test_durations").write_text(json.dumps(dict.fromkeys(nodeids[: 100 - missing], 0.1)))
    (tmp_path / "collected.json").write_text(json.dumps(nodeids))
    monkeypatch.setattr(sys.modules[__name__], "_ROOT", tmp_path)
    selected_nodeids = nodeids[:1] if selected else None
    if missing <= 10:
        _check_stored_durations(selected_nodeids)
    else:
        with pytest.raises(AssertionError, match=f"{missing} of 100 tests"):
            _check_stored_durations(selected_nodeids)


@pytest.mark.parametrize(("reused", "unstored"), [(False, 0), (True, 0), (True, 8), (True, 9)])
def test_stored_durations_skip_the_tests_the_queue_reuses(tmp_path, monkeypatch, reused, unstored):
    kept = [f"tests/test_probe.py::test_{i}" for i in range(80)]
    by_test = [f"tests/test_probe.py::test_{i}" for i in range(80, 90)]
    by_file = [f"tests/test_cached.py::test_{i}" for i in range(10)]
    (tmp_path / ".test_durations").write_text(json.dumps(dict.fromkeys(kept[unstored:], 0.1)))
    (tmp_path / "collected.json").write_text(json.dumps(kept + by_test + by_file))
    if reused:
        (tmp_path / "reused.json").write_text(json.dumps({"tests": by_test, "files": ["tests/test_cached.py"]}))
    monkeypatch.setattr(sys.modules[__name__], "_ROOT", tmp_path)
    if not reused:
        with pytest.raises(AssertionError, match="20 of 100 tests"):
            _check_stored_durations()
    elif unstored > 8:
        with pytest.raises(AssertionError, match=f"{unstored} of 80 tests"):
            _check_stored_durations()
    else:
        _check_stored_durations()


@pytest.mark.parametrize("missing", [0, 1, 2, 3, 10])
def test_stored_durations_judge_only_the_tests_a_selected_run_collected(tmp_path, monkeypatch, missing):
    ran = [f"tests/test_probe.py::test_{i}" for i in range(10)]
    (tmp_path / ".test_durations").write_text(json.dumps(dict.fromkeys(ran[missing:], 0.1)))
    monkeypatch.setattr(sys.modules[__name__], "_ROOT", tmp_path)
    monkeypatch.setattr(subprocess, "run", lambda *args, **kwargs: pytest.fail("a selected run collected the suite"))
    if missing <= 1:
        _check_stored_durations(ran)
    else:
        with pytest.raises(AssertionError, match=f"{missing} of 10 tests"):
            _check_stored_durations(ran)


@pytest.mark.parametrize("stored", [99, 100])
def test_a_selected_run_judges_its_new_tests_against_the_stored_suite(tmp_path, monkeypatch, stored):
    new = [f"tests/test_new.py::test_{i}" for i in range(10)]
    (tmp_path / ".test_durations").write_text(json.dumps({f"tests/test_old.py::test_{i}": 0.1 for i in range(stored)}))
    monkeypatch.setattr(sys.modules[__name__], "_ROOT", tmp_path)
    if stored >= 100:
        _check_stored_durations(new)
    else:
        with pytest.raises(AssertionError, match=f"10 of {stored} tests"):
            _check_stored_durations(new)


def test_a_selected_run_strips_the_xdist_group_suffix(tmp_path, monkeypatch):
    (tmp_path / ".test_durations").write_text(json.dumps({"tests/test_probe.py::test_0": 0.1}))
    monkeypatch.setattr(sys.modules[__name__], "_ROOT", tmp_path)
    _check_stored_durations(["tests/test_probe.py::test_0@ledger"])


def test_only_an_unsharded_session_hands_over_its_own_tests(monkeypatch):
    seen = []
    monkeypatch.setattr(sys.modules[__name__], "_check_stored_durations", seen.append)

    session = SimpleNamespace(items=[SimpleNamespace(nodeid="tests/test_probe.py::test_0")])

    def request(shard):
        return SimpleNamespace(config=SimpleNamespace(getoption=lambda name: shard), session=session)

    test_stored_durations_cover_the_collected_suite(request("1/4"))
    test_stored_durations_cover_the_collected_suite(request(None))
    assert seen == [None, ["tests/test_probe.py::test_0"]]


def test_dev_push_refreshes_stored_durations_unless_the_run_was_cancelled():
    job = _workflow()["jobs"]["refresh-durations"]
    assert job["needs"] == ["unit", "lint", "shard-check"]
    assert job["if"] == "${{ !cancelled() && github.event_name == 'push' }}"
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
        == 'python -m tests.refresh_durations --samples ~/durations-samples --ci-run "$GITHUB_RUN_ID" --ci 5\ntest -s .test_durations || { echo "::error::refresh durations: wrote no .test_durations"; exit 1; }\n'
    )
    assert "env" not in refresh
    assert save["uses"].startswith("actions/cache/save@")
    assert save["with"] == {"path": "~/durations-samples", "key": "durations-samples-${{ github.sha }}"}


def _sample(folder: Path, run: str, seconds: float) -> None:
    path = folder / run / "durations-3.12-1"
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


def test_refresh_durations_names_missing_shards_from_the_push_unit_matrix():
    workflow = yaml.safe_load((_ROOT / ".github/workflows/test.yml").read_text())
    matrix = workflow["jobs"]["unit"]["strategy"]["matrix"]
    assert matrix["python-version"] == list(refresh_durations.VERSIONS)
    assert matrix["shard"] == list(range(1, refresh_durations.SHARDS + 1))


def test_samples_refresh_keeps_earlier_durations_for_the_tests_of_a_failed_shard(tmp_path, monkeypatch):
    samples = tmp_path / "samples"
    for run, shard, durations in [
        ("11", 1, {"t.py::a@g": 1.0}),
        ("11", 2, {"t.py::b@g": 4.0, "t.py::gone@g": 5.0}),
        ("12", 1, {"t.py::a@g": 3.0}),
    ]:
        path = samples / run / f"durations-3.12-{shard}"
        path.mkdir(parents=True)
        (path / "durations.json").write_text(json.dumps(durations))
    monkeypatch.setattr(refresh_durations, "_ROOT", tmp_path)
    monkeypatch.setattr(refresh_durations, "collected_tests", lambda root: ["t.py::a", "t.py::b"])
    refresh_durations.main(["--samples", str(samples), "--ci-run", "12", "--ci", "5"])
    for name in (".test_durations", ".test_durations-3.12"):
        assert json.loads((tmp_path / name).read_text()) == {"t.py::a": 2.0, "t.py::b": 4.0}


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
    job = _workflow()["jobs"]["unit"]
    between = job["steps"][run_index + 1 : upload_index]
    assert [step["name"] for step in between] in (
        [],
        [
            "Mint the token that restores queue unit artifacts",
            "Restore queue shard durations",
            "Restore queue shard flakes",
            "Restore queue shard coverage",
        ],
    )
    for step in between:
        expected = "needs.split.outputs.source != ''"
        if step["name"] == "Restore queue shard coverage":
            expected += " && matrix.python-version == '3.12'"
        assert step["if"] == expected
    assert upload.get("if") in (None, "${{ !cancelled() && hashFiles('durations.json') != '' }}")
    assert upload["uses"] == "./.github/actions/retry-upload" or upload["uses"].startswith("actions/upload-artifact@")
    assert upload["with"]["name"] == "durations-${{ matrix.python-version }}-${{ matrix.shard }}"
    assert upload["with"]["path"].split() in (
        ["durations.json", "durations.sha256"],
        ["durations.json", "durations.sha256", "elapsed.json"],
    )


def test_unit_shards_retry_coverage_upload_without_changing_the_artifact():
    _, upload = _unit_step_index(lambda s: s.get("name") == "Upload coverage")
    assert upload["uses"] == "./.github/actions/retry-upload"
    assert upload["if"] == "matrix.python-version == '3.12'"
    assert upload["with"] == {
        "name": "coverage-${{ matrix.python-version }}-${{ matrix.shard }}",
        "path": ".coverage\njs-coverage/\ndep-inputs/\nreused-coverage/\n",
        "include-hidden-files": True,
        "if-no-files-found": "error",
        "retention-days": 7,
    }


@pytest.mark.parametrize("status", [0, 3])
def test_unit_step_records_elapsed_time_and_preserves_pytest_status(tmp_path, status):
    import json

    _, step = _unit_step_index(lambda item: item.get("name") == "Run tests")
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    (scripts / "__init__.py").touch()
    (scripts / "ci_cpu_quota.py").write_text("print(1)\n")
    (tmp_path / "pytest.py").write_text(
        f"def main(args):\n    open('durations.json', 'w').write('{{}}')\n    return {status}\n"
    )
    result = subprocess.run(
        ["bash", "-e", "-c", step["run"]],
        cwd=tmp_path,
        env={**os.environ, "PYTEST_ADDOPTS": ""},
        capture_output=True,
        text=True,
    )
    assert result.returncode == status, result.stdout + result.stderr
    assert (tmp_path / "durations.json").is_file()
    elapsed = json.loads((tmp_path / "elapsed.json").read_text())
    assert isinstance(elapsed["seconds"], (int, float))
    assert elapsed["seconds"] >= 0


def test_unit_shards_upload_elapsed_time_after_failed_pytest():
    _, upload = _unit_step_index(lambda step: step.get("name") == "Upload durations")
    assert upload["if"] == "${{ !cancelled() && hashFiles('durations.json') != '' }}"
    assert "elapsed.json" in upload["with"]["path"].split()


@pytest.mark.parametrize("status", [0, 3])
def test_selected_legs_record_elapsed_time_and_their_job_and_preserve_pytest_status(tmp_path, status):
    steps = _workflow()["jobs"]["unit-selected"]["steps"]
    step = next(item for item in steps if item.get("name") == "Run the selected tests")
    (tmp_path / "pytest.py").write_text(
        f"def main(args):\n    open('durations.json', 'w').write('{{}}')\n    return {status}\n"
    )
    result = subprocess.run(
        ["bash", "-e", "-c", step["run"]],
        cwd=tmp_path,
        env={**os.environ, "PYTEST_ADDOPTS": "", "FILES": "tests/test_a.py", "GITHUB_JOB": "unit-selected"},
        capture_output=True,
        text=True,
    )
    assert result.returncode == status, result.stdout + result.stderr
    elapsed = json.loads((tmp_path / "elapsed.json").read_text())
    assert elapsed["job"] == "unit-selected"
    assert elapsed["seconds"] >= 0


def test_selected_legs_upload_elapsed_time_beside_their_durations():
    steps = _workflow()["jobs"]["unit-selected"]["steps"]
    upload = next(step for step in steps if step.get("name") == "Upload selected test durations")
    assert upload["with"]["path"].split() == ["durations.json", "elapsed.json"]


def test_every_shard_records_the_hash_of_the_durations_it_splits_on():
    jobs = _workflow()["jobs"]
    adopt = next(s for s in jobs["split"]["steps"] if s.get("name") == "Adopt latest dev durations")
    upload = next(s for s in jobs["split"]["steps"] if s.get("name") == "Upload the chosen durations")
    assert " --hash durations.sha256 " in adopt["run"]
    assert upload["with"]["path"].split() == [".test_durations", "durations.sha256"]
    assert upload["with"]["include-hidden-files"] is True
    _, download = _unit_step_index(lambda s: s.get("name") == "Download the durations this run splits on")
    assert download["with"] == {
        "name": upload["with"]["name"],
        "client-id": "${{ secrets.TCC_CI_CLIENT_ID }}",
        "private-key": "${{ secrets.TCC_CI_APP_PRIVATE_KEY }}",
    }
    _, store = _unit_step_index(lambda s: s.get("name") == "Upload durations")
    assert "durations.sha256" in store["with"]["path"].split()


def test_ci_samples_are_one_per_shard_file_without_the_xdist_group_suffix(tmp_path):
    shards = {
        "durations-3.12-1": {"t.py::a@group": 1.0},
        "durations-3.12-2": {"t.py::a@group": 3.0, "t.py::b[x@y]": 2.0},
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
            path = folder / run / "durations-3.12-1"
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
        path = folder / "42" / "durations-3.12-1"
        path.mkdir(parents=True)
        (path / "durations.json").write_text(json.dumps({"t.py::a": 3.0}))

    monkeypatch.setattr(refresh_durations, "_ROOT", tmp_path)
    monkeypatch.setattr(refresh_durations, "collected_tests", lambda root: ["t.py::a"])
    monkeypatch.setattr(refresh_durations, "ci_download", download)
    refresh_durations.main(["--ci-run", "42"])
    assert json.loads((tmp_path / ".test_durations").read_text()) == {"t.py::a": 3.0}
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
            path = folder / run / "durations-3.12-1"
            path.mkdir(parents=True)
            (path / "durations.json").write_text(json.dumps(measured[run]))

    monkeypatch.setattr(refresh_durations, "_ROOT", tmp_path)
    monkeypatch.setattr(refresh_durations, "collected_tests", lambda root: ["t.py::a"])
    monkeypatch.setattr(refresh_durations, "ci_run_ids", lambda limit: ["42", "41", "40", "39"][:limit])
    monkeypatch.setattr(refresh_durations, "ci_download", download)
    refresh_durations.main(["--ci-run", "42", "--ci", "3"])
    assert json.loads((tmp_path / ".test_durations-3.12").read_text()) == {"t.py::a": 2.0, "t.py::new": 4.0}
    assert json.loads((tmp_path / ".test_durations").read_text()) == {"t.py::a": 2.0, "t.py::new": 4.0}


def test_ci_refresh_counts_the_passed_run_within_its_run_limit(tmp_path, monkeypatch):
    downloads = []

    def download(run_ids, folder):
        downloads.extend(run_ids)
        for run in run_ids:
            (folder / run / "durations-3.12-1").mkdir(parents=True)
            (folder / run / "durations-3.12-1" / "durations.json").write_text(json.dumps({"t.py::a": 1.0}))

    monkeypatch.setattr(refresh_durations, "_ROOT", tmp_path)
    monkeypatch.setattr(refresh_durations, "collected_tests", lambda root: ["t.py::a"])
    monkeypatch.setattr(refresh_durations, "ci_run_ids", lambda limit: ["42", "41", "40"][:limit])
    monkeypatch.setattr(refresh_durations, "ci_download", download)
    refresh_durations.main(["--ci-run", "39", "--ci", "3"])
    assert downloads == ["39", "42", "41"]


def test_ci_refresh_refuses_a_passed_run_that_kept_no_durations(tmp_path, monkeypatch):
    def download(run_ids, folder):
        (folder / "41" / "durations-3.12-1").mkdir(parents=True)
        (folder / "41" / "durations-3.12-1" / "durations.json").write_text(json.dumps({"t.py::a": 1.0}))

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
    assert job["needs"] in (
        ["mutation-plan", "mutation-stats"],
        ["mutation-plan", "mutation-stats", "admission"],
    )
    assert job["timeout-minutes"] == 26
    steps = job["steps"]
    checkout = next(step for step in steps if step.get("uses", "").startswith("actions/checkout"))
    assert checkout["with"]["fetch-depth"] == 0
    assert checkout["with"]["ref"] == "${{ github.event.pull_request.head.sha }}"
    run = next(step for step in steps if step.get("name") == "Mutate changed Python files")
    assert run["env"]["BASES"] == "${{ needs.mutation-plan.outputs.bases }}"
    assert (
        run["run"]
        == 'python -m scripts.ci_mutation --bases "$BASES" --budget 1080 --shard "$SHARD" --shards "$SHARDS"'
        + " --stats .mutation-stats"
    )
    artifact = next(step for step in steps if step.get("uses", "").startswith("actions/upload-artifact"))
    assert artifact["if"] == "always() && needs.mutation-plan.outputs.forwarded != 'true'"
    assert artifact["with"]["include-hidden-files"] is True
    assert artifact["with"]["name"] == "mutation-report-${{ matrix.shard }}"
    assert artifact["with"]["path"] == (
        "${{ job.status == 'success' && '.mutation-gate/report.json' || '.mutation-gate/' }}"
    )


def test_mutation_shards_come_from_a_plan_sized_on_stored_timings():
    jobs = _mutation_workflow()["jobs"]
    plan, mutation = jobs["mutation-plan"], jobs["mutation"]
    assert plan.get("needs", []) in ([], ["admission"])
    assert (plan["if"], mutation["if"]) in (
        (
            "${{ (github.event_name == 'pull_request' && github.base_ref == 'dev') || github.event_name == 'workflow_dispatch' }}",
            "${{ (github.event_name == 'pull_request' && github.base_ref == 'dev') || github.event_name == 'workflow_dispatch' }}",
        ),
        (
            "${{ !cancelled() && (github.event_name != 'workflow_dispatch' || needs.admission.result == 'success') && ((github.event_name == 'pull_request' && github.base_ref == 'dev') || github.event_name == 'workflow_dispatch') }}",
            "${{ !cancelled() && needs.mutation-plan.result == 'success' && (needs.mutation-plan.outputs.shards != '[0]' || needs.mutation-plan.outputs.stats_parts != '[0]') && needs.mutation-stats.result == 'success' && (github.event_name != 'workflow_dispatch' || needs.admission.result == 'success') && ((github.event_name == 'pull_request' && github.base_ref == 'dev') || github.event_name == 'workflow_dispatch') }}",
        ),
    )
    assert (
        plan["outputs"]["shards"] == "${{ steps.reuse.outputs.reused == 'true' && '[0]' || steps.plan.outputs.shards }}"
    )
    step = next(step for step in plan["steps"] if step.get("id") == "plan")
    assert step["if"] == "steps.reuse.outputs.reused != 'true'"
    assert step["run"] == "python -m scripts.ci_mutation." + 'plan --base "$BASE" --repository "$GITHUB_REPOSITORY"'
    assert step["env"] == {
        "BASE": "${{ inputs.base || 'origin/dev' }}",
        "GH_TOKEN": "${{ steps.app-token.outputs.token }}",
        "MUTATION_SHARD_CPU_QUOTA": "3",
    }
    names = [item.get("id") for item in plan["steps"]]
    mint = plan["steps"][names.index("app-token")]
    assert names.index("app-token") < names.index("plan")
    assert "if" not in mint
    assert mint["uses"] == "actions/create-github-app-token@v3.2.0"
    assert mint["with"] == {
        "client-id": "${{ secrets.TCC_CI_CLIENT_ID }}",
        "private-key": "${{ secrets.TCC_CI_APP_PRIVATE_KEY }}",
        "repositories": "${{ github.event.repository.name }}",
        "permission-actions": "read",
    }
    assert "permissions" not in plan
    assert plan["outputs"]["bases"] == "${{ steps.plan.outputs.bases }}"
    assert plan["outputs"]["browser"] == "${{ steps.plan.outputs.browser }}"
    for job in (mutation, jobs["mutation-stats"]):
        selection = next(
            step for step in job["steps"] if step.get("name") == "Download planned mutation test selection"
        )
        assert selection["with"]["name"] == "mutation-selection"
        assert not any(step.get("id") == "selection" for step in job["steps"])
    checkout = next(step for step in plan["steps"] if step.get("uses", "").startswith("actions/checkout"))
    assert checkout["with"] == {"fetch-depth": 0, "ref": "${{ github.event.pull_request.head.sha }}"}
    assert mutation["strategy"] == {
        "fail-fast": False,
        "matrix": {"shard": "${{ fromJSON(needs.mutation-plan.outputs.shards) }}"},
    }
    for name in ("Mutate changed Python files", "Mutate dispatched Python files"):
        run = next(step for step in mutation["steps"] if step.get("name") == name)
        assert run["env"]["SHARD"] == "${{ matrix.shard }}"
        assert run["env"]["SHARDS"] == "${{ strategy.job-total }}"
        assert run["env"]["BASES"] == "${{ needs.mutation-plan.outputs.bases }}"
        assert run["run"] == (
            'python -m scripts.ci_mutation --bases "$BASES" --budget 1080 --shard "$SHARD" --shards "$SHARDS"'
            " --stats .mutation-stats"
        )


def test_mutation_shards_reuse_stats_collected_once_in_planned_parts():
    jobs = _mutation_workflow()["jobs"]
    plan, stats, mutation = jobs["mutation-plan"], jobs["mutation-stats"], jobs["mutation"]
    assert (
        plan["outputs"]["stats_parts"]
        == "${{ steps.reuse.outputs.reused == 'true' && '[0]' || steps.plan.outputs.stats_parts }}"
    )
    assert stats["needs"] in ("mutation-plan", ["mutation-plan", "admission"])
    assert (stats["if"], mutation["if"]) in (
        (
            "${{ (github.event_name == 'pull_request' && github.base_ref == 'dev') || github.event_name == 'workflow_dispatch' }}",
            "${{ (github.event_name == 'pull_request' && github.base_ref == 'dev') || github.event_name == 'workflow_dispatch' }}",
        ),
        (
            "${{ !cancelled() && needs.mutation-plan.result == 'success' && needs.mutation-plan.outputs.forwarded != 'true' && (github.event_name != 'workflow_dispatch' || needs.admission.result == 'success') && ((github.event_name == 'pull_request' && github.base_ref == 'dev') || github.event_name == 'workflow_dispatch') }}",
            "${{ !cancelled() && needs.mutation-plan.result == 'success' && (needs.mutation-plan.outputs.shards != '[0]' || needs.mutation-plan.outputs.stats_parts != '[0]') && needs.mutation-stats.result == 'success' && (github.event_name != 'workflow_dispatch' || needs.admission.result == 'success') && ((github.event_name == 'pull_request' && github.base_ref == 'dev') || github.event_name == 'workflow_dispatch') }}",
        ),
    )
    assert stats["strategy"] == {
        "fail-fast": False,
        "matrix": {"part": "${{ fromJSON(needs.mutation-plan.outputs.stats_parts) }}"},
    }
    setup = [step for step in mutation["steps"] if not str(step.get("name", "")).startswith(("Mutate", "Upload"))]
    downloads = ("Download the shared mutation stats", "Download the reused preflight report")
    setup = [
        step
        for step in setup
        if step.get("name") not in downloads and step.get("with", {}).get("path") != ".mutation-stats-cache"
    ]
    stats_steps = [step for step in stats["steps"] if step.get("with", {}).get("path") != ".mutation-stats-cache"]
    assert stats_steps[: len(setup)] == setup
    collect = stats_steps[len(setup)]
    cache_steps = [step for step in stats["steps"] if step.get("with", {}).get("path") == ".mutation-stats-cache"]
    if cache_steps:
        restore, save = cache_steps
        assert restore["uses"] == "actions/cache/restore@v4"
        assert save["uses"] == "actions/cache/save@v4"
        assert restore["with"]["key"] == save["with"]["key"]
        assert restore["if"] == "needs.mutation-plan.outputs.forwarded != 'true'"
        assert save["if"] == "strategy.job-total == 1 && needs.mutation-plan.outputs.forwarded != 'true'"
        assert stats["steps"].index(restore) < stats["steps"].index(collect) < stats["steps"].index(save)
        assert stats["env"]["MUTATION_STATS_CACHE"] == "${{ github.workspace }}/.mutation-stats-cache"
    assert collect["env"]["BASES"] == "${{ needs.mutation-plan.outputs.bases }}"
    assert collect["env"]["PART"] == "${{ matrix.part }}"
    assert collect["env"]["PARTS"] == "${{ strategy.job-total }}"
    assert collect["run"] == (
        'mkdir -p .mutation-stats && touch ".mutation-stats/collected-$PART" && python -m scripts.ci_mutation'
        ' --bases "$BASES" --budget 1080 --stats .mutation-stats --stats-part "$PART" --stats-parts "$PARTS"'
    )
    upload = stats_steps[len(setup) + 1]
    assert upload["uses"] == "actions/upload-artifact@v4"
    assert upload["with"]["name"] == "mutation-stats-${{ matrix.part }}"
    assert upload["with"]["path"] == ".mutation-stats/"
    assert upload["with"]["include-hidden-files"] is True
    assert upload["with"]["if-no-files-found"] == "error"
    assert upload["with"]["overwrite"] is True
    evidence = stats_steps[len(setup) + 2]
    assert evidence["if"] == "failure()"
    assert evidence["with"]["name"] == "mutation-evidence-stats-${{ matrix.part }}"
    assert evidence["with"]["overwrite"] is True
    names = [step.get("name") for step in mutation["steps"]]
    download = mutation["steps"][names.index("Download the shared mutation stats")]
    assert names.index("Download the shared mutation stats") < names.index("Mutate changed Python files")
    assert download["uses"] == "actions/download-artifact@v4"
    assert download["with"] == {"pattern": "mutation-stats-*", "path": ".mutation-stats", "merge-multiple": True}


def test_a_one_part_one_shard_plan_mutates_inside_the_statistics_job():
    jobs = _mutation_workflow()["jobs"]
    stats, mutation = jobs["mutation-stats"], jobs["mutation"]
    single = "needs.mutation-plan.outputs.shards == '[0]' && needs.mutation-plan.outputs.stats_parts == '[0]'"
    assert (
        "(needs.mutation-plan.outputs.shards != '[0]' || needs.mutation-plan.outputs.stats_parts != '[0]')"
        in mutation["if"]
    )
    assert jobs["gate-required"]["steps"][0]["env"]["INLINE"] == "${{ " + single + " }}"
    names = [step.get("name") for step in stats["steps"]]
    mutate = stats["steps"][names.index("Mutate the only shard in the statistics job")]
    assert mutate["id"] == "mutate"
    assert mutate["if"] == f"{single} && needs.mutation-plan.outputs.forwarded != 'true'"
    assert mutate["env"]["BASES"] == "${{ needs.mutation-plan.outputs.bases }}"
    assert mutate["run"] == (
        'python -m scripts.ci_mutation --bases "$BASES" --budget 1080 --shard 0 --shards 1 --stats .mutation-stats'
    )
    assert names.index("Upload mutation stats part") < names.index(mutate["name"])
    report = stats["steps"][names.index("Upload complete inline mutation evidence")]
    assert report["if"] == "always() && steps.mutate.outcome != 'skipped'"
    assert report["with"]["name"] == "mutation-report-0"
    assert (
        report["with"]["path"] == "${{ job.status == 'success' && '.mutation-gate/report.json' || '.mutation-gate/' }}"
    )
    assert names.index(mutate["name"]) < names.index(report["name"])


def test_every_pull_request_stats_part_restores_one_merged_cache_saved_once_after_all_parts_finish():
    jobs = _mutation_workflow()["jobs"]
    skipped = "needs.mutation-plan.outputs.forwarded != 'true'"
    key = "mutation-test-stats-v2-${{ runner.os }}-${{ github.run_id }}-${{ github.run_attempt }}"
    cached = {
        "path": ".mutation-stats-cache",
        "key": key,
        "restore-keys": "mutation-test-stats-v2-${{ runner.os }}-",
    }
    steps = jobs["mutation-stats"]["steps"]
    names = [step.get("name") for step in steps]
    restore = steps[names.index("Restore compatible test stats")]
    assert restore["uses"] == "actions/cache/restore@v4"
    assert restore["if"] == skipped
    assert restore["with"] == cached
    single = steps[names.index("Save the merged test stats")]
    assert single["uses"] == "actions/cache/save@v4"
    assert single["if"] == f"strategy.job-total == 1 && {skipped}"
    assert single["with"] == {"path": ".mutation-stats-cache", "key": key}
    fresh = steps[names.index("Keep only the test stats this part wrote")]
    assert fresh["if"] == f"strategy.job-total != 1 && {skipped}"
    assert fresh["env"] == {"PART": "${{ matrix.part }}"}
    assert fresh["run"] == (
        "mkdir -p .mutation-stats-cache .mutation-stats-fresh && find .mutation-stats-cache -maxdepth 1 -type f"
        ' -newer ".mutation-stats/collected-$PART" -exec cp -p {} .mutation-stats-fresh/ \\;'
    )
    part = steps[names.index("Upload this part's test stats cache")]
    assert part["if"] == f"strategy.job-total != 1 && {skipped}"
    assert part["uses"] == "actions/upload-artifact@v4"
    assert part["with"] == {
        "name": "mutation-test-stats-cache-${{ matrix.part }}",
        "path": ".mutation-stats-fresh/",
        "include-hidden-files": True,
        "if-no-files-found": "ignore",
        "retention-days": 1,
        "overwrite": True,
    }
    order = [
        restore["name"],
        "Collect one stats part for every mutation shard",
        single["name"],
        fresh["name"],
        part["name"],
    ]
    assert [names.index(name) for name in order] == sorted(names.index(name) for name in order)
    assert [step for step in steps if step.get("uses", "").startswith("actions/cache/save")] == [single]
    first = f"strategy.job-index == 0 && needs.mutation-plan.outputs.stats_parts != '[0]' && {skipped}"
    mutation = jobs["mutation"]["steps"]
    mutation_names = [step.get("name") for step in mutation]
    base = mutation[mutation_names.index("Restore compatible test stats")]
    assert base["uses"] == "actions/cache/restore@v4"
    assert base["if"] == first
    assert base["with"] == cached
    download = mutation[mutation_names.index("Download every part's test stats cache")]
    assert download["if"] == first
    assert download["uses"] == "actions/download-artifact@v4"
    assert download["with"] == {
        "pattern": "mutation-test-stats-cache-*",
        "path": ".mutation-stats-cache",
        "merge-multiple": True,
    }
    merged = mutation[mutation_names.index("Save the merged test stats")]
    assert merged["uses"] == "actions/cache/save@v4"
    assert merged["if"] == f"{first} && hashFiles('.mutation-stats-cache/**') != ''"
    assert merged["with"] == {"path": ".mutation-stats-cache", "key": key}
    assert mutation_names.index(base["name"]) == 1
    order = [base["name"], download["name"], merged["name"], "Install dependencies", "Mutate changed Python files"]
    assert [mutation_names.index(name) for name in order] == sorted(mutation_names.index(name) for name in order)


PLANNER_PACKAGES = {"mutmut": "mutmut", "pytest": "pytest", "pyyaml": "yaml", "tomlkit": "tomlkit"}


def _planner_imports(path: Path) -> set[str]:
    found, pending = set(), [ast.parse(path.read_text())]
    while pending:
        for child in ast.iter_child_nodes(pending.pop()):
            if isinstance(child, ast.Import):
                found |= {alias.name for alias in child.names}
            elif isinstance(child, ast.ImportFrom) and child.level == 0:
                found |= {child.module} | {f"{child.module}.{alias.name}" for alias in child.names}
            elif isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                # A function's own third party import loads only when it runs; its first party ones decide what else the planner reads.
                found |= {
                    node.module
                    for node in ast.walk(child)
                    if isinstance(node, ast.ImportFrom)
                    and node.level == 0
                    and node.module.split(".")[0] in ("scripts", "tests")
                }
            else:
                pending.append(child)
    return found


def _planner_third_party() -> set[str]:
    seen, third, queue = set(), set(), ["scripts.ci_mutation.plan", "scripts.ci_mutation.__main__"]
    while queue:
        name = queue.pop()
        if name in seen:
            continue
        seen.add(name)
        base = _ROOT.joinpath(*name.split("."))
        path = next((p for p in (base.with_suffix(".py"), base / "__init__.py") if p.is_file()), None)
        for dep in _planner_imports(path) if path else ():
            parts = dep.split(".")
            if parts[0] in ("scripts", "tests"):
                queue += [".".join(parts[:i]) for i in range(1, len(parts) + 1)]
            elif parts[0] not in sys.stdlib_module_names:
                third.add(parts[0])
    return third


def test_mutation_plan_installs_only_what_the_planner_imports_at_the_project_pins():
    plan = yaml.safe_load((_ROOT / ".github/workflows/test.yml").read_text())["jobs"]["mutation-plan"]
    (install,) = [step for step in plan["steps"] if step.get("name") == "Install dependencies"]
    project = tomllib.loads((_ROOT / "pyproject.toml").read_text())["project"]
    declared = {re.split(r"[<>=!\[]", spec)[0].lower(): spec for spec in project["optional-dependencies"]["dev"]}
    declared |= {re.split(r"[<>=!\[]", spec)[0].lower(): spec for spec in project["dependencies"]}
    specs = [declared[name] for name in sorted(PLANNER_PACKAGES)]
    assert (
        install["run"]
        == 'bash "$RUNNER_TEMP/workflow/.github/scripts/pypi-retry.sh" "Install dependencies" -- uv pip install --system '
        + " ".join(f'"{spec}"' for spec in specs)
    )
    assert _planner_third_party() == set(PLANNER_PACKAGES.values())


def test_pull_request_mutation_reuses_the_preflight_of_its_exact_head_from_the_protected_grader():
    jobs = _mutation_workflow()["jobs"]
    plan, stats, mutation = jobs["mutation-plan"], jobs["mutation-stats"], jobs["mutation"]
    assert plan["outputs"]["reused"] == "${{ steps.reuse.outputs.reused || 'false' }}"
    names = [step.get("name") for step in plan["steps"]]
    grader = plan["steps"][names.index("Check out the protected grader")]
    assert grader["env"] == {"GRADER": "${{ github.event.pull_request.base.sha }}"}
    assert 'git worktree add --detach "$RUNNER_TEMP/grader" FETCH_HEAD' in grader["run"]
    decide = plan["steps"][names.index("Reuse the branch preflight report graded on this head and base")]
    assert decide["id"] == "reuse"
    assert decide["if"] == grader["if"] == "github.event_name == 'pull_request'"
    assert decide["env"]["GH_TOKEN"] == "${{ steps.app-token.outputs.token }}"
    assert decide["env"]["HEAD_SHA"] == "${{ github.event.pull_request.head.sha }}"
    assert 'base=$(git merge-base origin/dev "$HEAD_SHA")' in decide["run"]
    assert 'PYTHONPATH="$RUNNER_TEMP/grader" python -P -m scripts.ci_mutation.reuse' in decide["run"]
    assert decide["run"].rstrip().endswith('--head "$HEAD_SHA" --base "$base" --grader "$GRADER"')
    assert decide["env"]["GRADER"] == grader["env"]["GRADER"]
    assert decide["run"].startswith('base=$(git merge-base origin/dev "$HEAD_SHA")')
    assert "||" not in decide["run"]
    assert plan["outputs"]["forwarded"] == (
        "${{ (steps.reuse.outputs.reused == 'true' || steps.plan.outputs.changed == '0') && 'true' || 'false' }}"
    )
    empty = plan["steps"][names.index("Grade the empty change set")]
    assert empty["if"] in (
        "steps.plan.outputs.changed == '0'",
        "steps.reuse.outputs.reused != 'true' && steps.plan.outputs.changed == '0'",
    )
    assert empty["env"] == {"BASES": "${{ steps.plan.outputs.bases }}"}
    assert empty["run"] == 'python -m scripts.ci_mutation --bases "$BASES" --budget 1080'
    upload = plan["steps"][names.index("Forward the reused or empty mutation report")]
    assert upload["if"] == "always() && (steps.reuse.outputs.reused == 'true' || steps.plan.outputs.changed == '0')"
    assert upload["with"]["name"] == "mutation-report-0"
    assert upload["with"]["path"] == ".mutation-gate/"
    assert upload["with"]["if-no-files-found"] == "error"
    assert names[-2:] == ["Grade the empty change set", "Forward the reused or empty mutation report"]
    order = ["app-token", "reuse", "plan"]
    ids = [step.get("id") for step in plan["steps"]]
    assert [ids.index(name) for name in order] == sorted(ids.index(name) for name in order)
    install = plan["steps"][names.index("Install dependencies")]
    assert install["if"] == "steps.reuse.outputs.reused != 'true'"
    skipped = "needs.mutation-plan.outputs.forwarded != 'true'"
    unguarded = (
        "needs.mutation-plan.outputs.browser == 'true'",
        "failure() && steps.browser.outcome == 'failure'",
        "failure()",
        f"github.event_name == 'pull_request' && {skipped}",
        f"github.event_name == 'workflow_dispatch' && {skipped}",
        f"always() && {skipped}",
        f"{skipped} && needs.mutation-plan.outputs.selection == 'true'",
        f"strategy.job-total == 1 && {skipped}",
        f"strategy.job-total != 1 && {skipped}",
        f"strategy.job-index == 0 && needs.mutation-plan.outputs.stats_parts != '[0]' && {skipped}",
        f"strategy.job-index == 0 && needs.mutation-plan.outputs.stats_parts != '[0]' && {skipped}"
        " && hashFiles('.mutation-stats-cache/**') != ''",
        f"needs.mutation-plan.outputs.shards == '[0]' && needs.mutation-plan.outputs.stats_parts == '[0]' && {skipped}",
        "always() && steps.mutate.outcome != 'skipped'",
    )
    for job in (stats, mutation):
        assert all(step["if"] == skipped or step["if"] in unguarded for step in job["steps"])
    steps = {step.get("name"): step for step in mutation["steps"]}
    assert steps["Mutate changed Python files"]["if"] == f"github.event_name == 'pull_request' && {skipped}"
    assert steps["Mutate dispatched Python files"]["if"] == f"github.event_name == 'workflow_dispatch' && {skipped}"
    assert "Download the reused preflight report" not in steps
    assert steps["Upload complete mutation evidence"]["if"] == f"always() && {skipped}"


def test_jobs_on_the_pull_request_head_run_local_actions_from_the_workflow_revision():
    jobs = yaml.safe_load((_ROOT / ".github/workflows/test.yml").read_text())["jobs"]
    head = "${{ github.event.pull_request.head.sha }}"
    local = {
        name: [step for step in job["steps"] if step.get("uses", "").startswith("./.")]
        for name, job in jobs.items()
        if name.startswith("mutation") and any(s.get("with", {}).get("ref") == head for s in job.get("steps", []))
    }
    assert {"mutation-stats", "mutation"} <= set(local)
    assert all(not steps for name, steps in local.items() if name not in {"mutation-stats", "mutation"})
    for name in ("mutation-stats", "mutation"):
        steps = jobs[name]["steps"]
        names = [step.get("name") for step in steps]
        take = steps[names.index("Take the local actions from the workflow revision")]
        assert take["if"] == "needs.mutation-plan.outputs.forwarded != 'true'"
        assert take["run"].splitlines() == [
            "mkdir -p .workflow-revision",
            'git archive "$GITHUB_SHA" .github/actions | tar -x -C .workflow-revision',
        ]
        assert names.index("Take the helper scripts from the workflow revision") < steps.index(take)
        assert {step["uses"] for step in local[name]} == {
            "./.workflow-revision/.github/actions/nats-server",
            "./.workflow-revision/.github/actions/browser-cache",
        }
        assert all(steps.index(take) < steps.index(step) for step in local[name])
        assert all("hashFiles" not in step.get("if", "") for step in local[name])


def test_a_head_that_predates_the_local_actions_gets_them_from_the_workflow_revision(tmp_path):
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t"}
    env["GIT_COMMITTER_EMAIL"] = "t@t"

    def git(*args):
        command = ["git", "-c", "commit.gpgsign=false", *args]
        return subprocess.run(command, cwd=tmp_path, env=env, check=True, capture_output=True, text=True).stdout

    git("init", "-q", "-b", "dev")
    (tmp_path / "README").write_text("old\n")
    git("add", ".")
    git("commit", "-qm", "head without the actions")
    head = git("rev-parse", "HEAD").strip()
    for action in ("nats-server", "browser-cache"):
        (tmp_path / ".github/actions" / action).mkdir(parents=True)
        (tmp_path / ".github/actions" / action / "action.yml").write_text(f"name: {action}\n")
    git("add", ".")
    git("commit", "-qm", "workflow revision")
    workflow_sha = git("rev-parse", "HEAD").strip()
    git("checkout", "-q", "--detach", head)
    assert not (tmp_path / ".github/actions").exists()

    steps = yaml.safe_load((_ROOT / ".github/workflows/test.yml").read_text())["jobs"]["mutation-stats"]["steps"]
    take = next(s for s in steps if s.get("name") == "Take the local actions from the workflow revision")
    run_env = {**env, "GITHUB_SHA": workflow_sha}
    subprocess.run(["bash", "-euo", "pipefail", "-c", take["run"]], cwd=tmp_path, env=run_env, check=True)

    for step in steps:
        if step.get("uses", "").startswith("./."):
            assert (tmp_path / step["uses"] / "action.yml").read_text() == f"name: {Path(step['uses']).name}\n"
    assert git("status", "--porcelain", "--", ".github") == ""
