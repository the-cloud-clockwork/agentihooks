import ast
import json
import os
import queue
import re
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest
import yaml

from scripts.ci_branch_coverage import ARTIFACT
from scripts.ci_branch_plan import LEG, LEG_JOB, PREFIX, TEST_STEPS

pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github/workflows/test.yml"
LEG_JOBS = ("unit", "unit-selected")
WRITER = ("Record this leg's passed tests", "Upload this leg's branch record", "Upload this leg's branch coverage")
SAME_REPO = {
    "github.event_name": "pull_request",
    "github.base_ref": "dev",
    "github.event.pull_request.head.repo.full_name": "o/r",
    "github.repository": "o/r",
}


def _jobs() -> dict:
    return yaml.safe_load(WORKFLOW.read_text())["jobs"]


def _step(job: str, name: str) -> dict:
    return next(step for step in _jobs()[job]["steps"] if step.get("name") == name)


def _truth(node: ast.AST) -> bool:
    if isinstance(node, ast.BoolOp):
        values = [_truth(value) for value in node.values]
        return all(values) if isinstance(node.op, ast.And) else any(values)
    if isinstance(node, ast.Compare):
        (operator,), (right,) = node.ops, node.comparators
        equal = node.left.value == right.value
        return equal if isinstance(operator, ast.Eq) else not equal
    raise AssertionError(f"cannot evaluate {ast.dump(node)}")


def _evaluate(expression: str, values: dict) -> bool:
    python = re.sub(
        r"\b(?:github|steps|needs|matrix)(?:\.[\w-]+)+", lambda m: repr(values.get(m.group(0), "")), expression
    )
    return _truth(ast.parse(python.replace("&&", " and ").replace("||", " or "), mode="eval").body)


def _name(template: str, leg: str = "7", attempt: str = "2") -> str:
    return (
        template.replace("${{ matrix.python-version }}", "3.12")
        .replace("${{ env.LEG }}", leg)
        .replace("${{ github.run_attempt }}", attempt)
    )


@pytest.mark.parametrize("job", LEG_JOBS)
@pytest.mark.parametrize("name", WRITER)
def test_the_record_and_its_uploads_run_after_a_cancel_on_same_repo_pull_requests_only(job, name):
    step = _step(job, name)
    assert step["if"].startswith("always() && ")
    assert "cancelled()" not in step["if"]
    condition = re.sub(r" && hashFiles\('[^']+'\) != ''$", "", step["if"].removeprefix("always() && "))
    ran = {**SAME_REPO, "steps.tests.outcome": "cancelled"}
    assert _evaluate(condition, ran)
    for event in ("merge_group", "push", "workflow_dispatch"):
        assert not _evaluate(condition, {**ran, "github.event_name": event})
    assert not _evaluate(condition, {**ran, "github.event.pull_request.head.repo.full_name": "fork/r"})


@pytest.mark.parametrize("job", LEG_JOBS)
@pytest.mark.parametrize(("outcome", "recorded"), [("success", True), ("failure", True), ("cancelled", True)])
def test_a_leg_records_whenever_its_test_step_ran_even_before_its_first_result(job, outcome, recorded):
    step = _step(job, "Record this leg's passed tests")
    assert "hashFiles" not in step["if"]
    tests = next(item for item in _jobs()[job]["steps"] if item.get("id") == "tests")
    assert tests["name"] in TEST_STEPS
    condition = step["if"].removeprefix("always() && ")
    assert _evaluate(condition, {**SAME_REPO, "steps.tests.outcome": outcome}) is recorded
    assert not _evaluate(condition, {**SAME_REPO, "steps.tests.outcome": "skipped"})


@pytest.mark.parametrize("job", LEG_JOBS)
def test_a_leg_uploads_its_coverage_before_the_record_that_points_at_it(job):
    names = [step.get("name") for step in _jobs()[job]["steps"]]
    assert (
        names.index("Record this leg's passed tests")
        < names.index("Upload this leg's branch coverage")
        < names.index("Upload this leg's branch record")
    )


def test_the_planner_names_the_jobs_and_steps_the_workflow_runs():
    jobs = _jobs()
    unit = jobs["unit"]["name"].replace("${{ matrix.python-version }}", "3.12").replace("${{ matrix.shard }}", "4")
    assert LEG_JOB.fullmatch(unit)
    assert "name" not in jobs["unit-selected"]
    assert list(jobs["unit-selected"]["strategy"]["matrix"]) == ["python-version", "leg"]
    assert LEG_JOB.fullmatch("unit-selected (3.12, 4)")
    assert {_step("unit", "Run tests")["name"], _step("unit-selected", "Run the selected tests")["name"]} == set(
        TEST_STEPS
    )


@pytest.mark.parametrize("job", LEG_JOBS)
def test_both_uploads_survive_a_cancel_and_carry_the_names_the_grader_reads(job):
    record = _step(job, "Upload this leg's branch record")
    coverage = _step(job, "Upload this leg's branch coverage")
    for upload in (record, coverage):
        assert upload["uses"] == "./.github/actions/retry-upload"
        assert upload["with"]["after-cancel"] is True
        assert upload["if"].startswith("always() && ")
        assert upload["with"]["retention-days"] == 7
    assert record["with"]["path"] == "branch-record/record.json"
    assert _name(record["with"]["name"]).startswith(PREFIX)
    assert LEG.fullmatch(_name(record["with"]["name"]).removeprefix(PREFIX).rsplit("-", 1)[0])
    assert ARTIFACT.fullmatch(_name(coverage["with"]["name"]))
    assert coverage["with"]["path"].split() == [".coverage", ".coverage.*", "elapsed.json", "js-coverage/"]
    assert coverage["with"]["include-hidden-files"] is True


def test_every_leg_is_named_by_plain_digits():
    jobs = _jobs()
    assert jobs["unit"]["env"]["LEG"] == "${{ matrix.shard }}"
    assert jobs["unit-selected"]["env"]["LEG"] == "${{ matrix.leg }}"
    assert all(isinstance(shard, int) for shard in jobs["unit"]["strategy"]["matrix"]["shard"])


@pytest.mark.parametrize("job", LEG_JOBS)
def test_the_writer_plugins_load_on_the_command_line_with_their_folders(job):
    run = _step(job, "Run tests" if job == "unit" else "Run the selected tests")
    assert "-p tests.pass_record" in run["run"]
    assert "pass_record" not in run["env"]["PYTEST_ADDOPTS"]
    assert "ci-results" in run["env"]["CI_RESULTS"]
    assert _evaluate(
        run["env"]["CI_RESULTS"].removeprefix("${{ ").split(" && format(")[0],
        {
            **SAME_REPO,
            "matrix.python-version": "3.12",
        },
    )
    if job == "unit-selected":
        assert "--cov-context=test -p tests.dep_inputs -p tests.pass_record" in run["run"]
        assert "dep-inputs" in run["env"]["DEP_INPUTS_DIR"]
        assert run["env"]["CI_REUSED"] == "${{ steps.reuse.outputs.reused }}"


@pytest.mark.parametrize("started", [True, False], ids=["results", "cancelled-before-the-first-test"])
def test_the_record_step_passes_every_flag_the_writer_parses(tmp_path, started):
    step = _step("unit", "Record this leg's passed tests")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "python").write_text(f'#!/usr/bin/env bash\nexec {sys.executable} "$@"\n')
    (bin_dir / "python").chmod(0o755)
    leg = tmp_path / "leg"
    leg.mkdir()
    if started:
        (leg / "ci-results").mkdir()
        (leg / "dep-inputs").mkdir()
        line = {"test": "tests/test_a.py::t", "outcome": "passed", "pid": 1}
        (leg / "ci-results/1.jsonl").write_text(json.dumps(line))
    env = {
        **os.environ,
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "PYTHONPATH": str(ROOT),
        "GITHUB_SHA": "a" * 40,
        "HEAD_SHA": "b" * 40,
        "PULL": "12",
        "GITHUB_RUN_ID": "99",
        "GITHUB_RUN_ATTEMPT": "2",
        "LEG": "7",
        "ENVIRONMENT": "venv-key",
    }
    result = subprocess.run(["bash", "-e", "-c", step["run"]], cwd=leg, env=env, capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr
    record = json.loads((leg / "branch-record/record.json").read_text())
    assert {key: record[key] for key in ("run", "attempt", "leg", "commit", "head", "pull", "environment")} == {
        "run": 99,
        "attempt": 2,
        "leg": "7",
        "commit": "a" * 40,
        "head": "b" * 40,
        "pull": 12,
        "environment": "venv-key",
    }
    assert "Branch record for leg 7" in result.stdout


def _planner_repo(tmp_path: Path, branch_planner: bool) -> Path:
    workspace = tmp_path / "workspace"
    files = {
        ".github/scripts/pypi-retry.sh": "exit 0\n",
        "scripts/__init__.py": "",
        "scripts/ci_budget/__init__.py": "",
        "scripts/ci_budget/rate_limit.py": "print('2026-10-27T00:00:00Z')\n",
        "scripts/ci_test_plan.py": (
            "import os\nopen(os.environ['GITHUB_OUTPUT'], 'a').write(f\"reused={os.environ['FAKE_REUSED']}\\n\")\n"
        ),
        "tests/__init__.py": "",
        "tests/reused_coverage.py": "",
    }
    if branch_planner:
        files["scripts/ci_branch_plan.py"] = (
            "import json, os, sys\n"
            "open(os.environ['MARKS'] + '/branch.json', 'w').write(json.dumps(sys.argv[1:]))\n"
            "open(os.environ['GITHUB_OUTPUT'], 'a').write('branch=2\\nbranch_runs=5\\n')\n"
        )
    for name, text in files.items():
        (workspace / name).parent.mkdir(parents=True, exist_ok=True)
        (workspace / name).write_text(text)
    git = ["git", "-C", str(workspace), "-c", "user.name=t", "-c", "user.email=t@t"]
    subprocess.run([*git, "init", "-q"], check=True)
    subprocess.run([*git, "add", "."], check=True)
    subprocess.run([*git, "commit", "-qm", "grader"], check=True)
    return workspace


def _plan(
    tmp_path: Path, event: str, reused: str = "0", branch_planner: bool = True, bash_env: str = ""
) -> tuple[Path, str]:
    workspace = _planner_repo(tmp_path, branch_planner)
    runner, marks = tmp_path / "runner", tmp_path / "marks"
    runner.mkdir()
    marks.mkdir()
    env = {
        **os.environ,
        "RUNNER_TEMP": str(runner),
        "GITHUB_WORKSPACE": str(workspace),
        "GITHUB_EVENT_NAME": event,
        "GITHUB_REPOSITORY": "o/r",
        "GITHUB_RUN_ID": "99",
        "GITHUB_SHA": "c" * 40,
        "GRADER": "HEAD",
        "PYTHON_PATH": sys.executable,
        "PULL": "12",
        "HEAD_REF": "feature",
        "HEAD_REPO": "o/r",
        "ENVIRONMENT": "venv-key",
        "HOME": str(tmp_path),
        "FAKE_REUSED": reused,
        "MARKS": str(marks),
    }
    if bash_env:
        (tmp_path / "bash_env").write_text(bash_env)
        env["BASH_ENV"] = str(tmp_path / "bash_env")
    run = _step("test-count", "Plan the tests whose inputs did not change")["run"]
    subprocess.run(["bash", "-e", "-c", run], cwd=workspace, env=env, check=True, capture_output=True)
    deadline = time.monotonic() + 30
    while not (runner / "plan.status").is_file() and time.monotonic() < deadline:
        time.sleep(0.05)
    log = (runner / "plan.log").read_text()
    assert (runner / "plan.status").read_text().strip() == "0", log
    return marks / "branch.json", (runner / "plan-outputs.txt").read_text() + log


@pytest.mark.parametrize("event", ["merge_group", "workflow_dispatch"])
def test_the_branch_planner_never_runs_outside_a_pull_request(tmp_path, event):
    called, outputs = _plan(tmp_path, event, reused="3")
    assert not called.exists()
    assert "branch=" not in outputs


@pytest.mark.parametrize(("reused", "dev_plan"), [("0", False), ("3", True)])
def test_a_pull_request_plans_from_its_earlier_runs_after_the_dev_plan(tmp_path, reused, dev_plan):
    called, outputs = _plan(tmp_path, "pull_request", reused=reused)
    argv = json.loads(called.read_text())
    workspace, runner = tmp_path / "workspace", tmp_path / "runner"
    flags = dict(zip(argv[::2], argv[1::2], strict=True))
    assert flags == {
        "--repository": "o/r",
        "--pull": "12",
        "--head-ref": "feature",
        "--head-repo": "o/r",
        "--run": "99",
        "--created": "2026-10-27T00:00:00Z",
        "--environment": "venv-key",
        **({"--dev-plan": str(workspace / "reused-tests.json")} if dev_plan else {}),
        "--root": str(workspace),
        "--out": str(runner / "reused-branch.json"),
    }
    assert "branch=2" in outputs


def test_a_grader_without_the_branch_planner_reuses_nothing_from_earlier_runs(tmp_path):
    called, outputs = _plan(tmp_path, "pull_request", branch_planner=False)
    assert not called.exists()
    assert "The grader has no branch planner" in outputs


def test_a_reader_never_sees_the_plan_status_before_its_writer_finishes(tmp_path):
    slow_echo = 'echo() { [[ "${1:-}" =~ ^[0-9]+$ ]] && sleep 1; builtin echo "$@"; }\n'
    _plan(tmp_path, "pull_request", bash_env=slow_echo)


def test_the_planners_run_unbuffered_so_their_lines_reach_the_log_as_written():
    assert _step("test-count", "Plan the tests whose inputs did not change")["env"]["PYTHONUNBUFFERED"] == "1"


@pytest.mark.wall_clock
@pytest.mark.parametrize("status", ["0", "3"])
def test_the_wait_prints_each_planner_line_while_the_planner_still_runs(tmp_path, status):
    runner, release = tmp_path / "runner", tmp_path / "release"
    runner.mkdir()
    (runner / "plan-outputs.txt").write_text("reused=0\n")
    script = f'echo planning; until [[ -f "{release}" ]]; do sleep 0.1; done; echo planned; echo {status} > "{runner}/plan.status"'
    with (runner / "plan.log").open("w") as log:
        planner = subprocess.Popen(["bash", "-c", script], stdout=log, stderr=subprocess.STDOUT)
    (runner / "plan.pid").write_text(str(planner.pid))
    env = {**os.environ, "RUNNER_TEMP": str(runner), "GITHUB_OUTPUT": str(tmp_path / "outputs")}
    run = _step("test-count", "Wait for the test plan")["run"]
    wait = subprocess.Popen(["bash", "-e", "-c", run], env=env, stdout=subprocess.PIPE, text=True)
    lines: queue.Queue[str] = queue.Queue()
    threading.Thread(target=lambda: [lines.put(line) for line in wait.stdout], daemon=True).start()
    try:
        assert lines.get(timeout=20) == "planning\n"
        assert wait.poll() is None
    finally:
        release.touch()
    assert wait.wait(timeout=30) == int(status)
    planner.wait()
    assert lines.get(timeout=10) == "planned\n"
    assert (tmp_path / "outputs").read_text() == "reused=0\n"


@pytest.mark.parametrize(
    ("event", "branch", "uploaded"),
    [
        ("pull_request", "4", True),
        ("pull_request", "0", False),
        ("pull_request", "", False),
        ("merge_group", "4", False),
    ],
)
def test_the_branch_plan_is_published_only_when_it_reuses_a_test(event, branch, uploaded):
    upload = _step("test-count", "Upload the pull request runs' test plan")
    assert upload["with"]["name"] == "reused-branch-${{ matrix.python-version }}"
    assert upload["with"]["path"] == "${{ runner.temp }}/reused-branch.json"
    values = {"github.event_name": event, "steps.plan.outputs.branch": branch}
    assert _evaluate(upload["if"], values) is uploaded


@pytest.mark.parametrize(("reused", "branch", "deselects"), [("0", "0", False), ("3", "0", True), ("0", "2", True)])
def test_the_deselect_and_its_upload_run_when_either_plan_reuses_a_test(reused, branch, deselects):
    values = {"steps.plan.outputs.reused": reused, "steps.plan.outputs.branch": branch}
    for name in ("Deselect the reused tests", "Upload the deselected tests"):
        assert _evaluate(_step("test-count", name)["if"], values) is deselects


def _deselect(tmp_path: Path, dev: bool) -> dict:
    collected = ["tests/test_a.py::one", "tests/test_a.py::two", "tests/test_b.py::one"]
    runner, workspace = tmp_path / "runner", tmp_path / "workspace"
    (runner / "planner/tests").mkdir(parents=True)
    (runner / "planner/tests/__init__.py").write_text("")
    (runner / "planner/tests/dep_manifest.py").write_text((ROOT / "tests/dep_manifest.py").read_text())
    workspace.mkdir()
    (workspace / "reused-tests.json").write_text(json.dumps({"tests": collected[:2]}))
    (runner / "reused-branch.json").write_text(json.dumps({"version": 1, "runs": [], "tests": collected[2:]}))
    (runner / "collected.json").write_text(json.dumps(collected))
    env = {
        **os.environ,
        "RUNNER_TEMP": str(runner),
        "GITHUB_WORKSPACE": str(workspace),
        "PYTHON_PATH": sys.executable,
        "DEV_PLAN": str(workspace / "reused-tests.json") if dev else "",
        "BRANCH_PLAN": str(runner / "reused-branch.json"),
    }
    run = _step("test-count", "Deselect the reused tests")["run"]
    subprocess.run(["bash", "-e", "-c", run], cwd=workspace, env=env, check=True, capture_output=True)
    return json.loads((runner / "reused.json").read_text())


def test_the_deselect_takes_both_plans_and_names_the_branch_only_tests(tmp_path):
    chosen = _deselect(tmp_path, dev=True)
    assert chosen["tests"] == ["tests/test_a.py::one", "tests/test_a.py::two", "tests/test_b.py::one"]
    assert chosen["branch"] == ["tests/test_b.py::one"]


def test_a_dev_plan_file_in_the_head_is_ignored_when_the_dev_planner_reused_nothing(tmp_path):
    chosen = _deselect(tmp_path, dev=False)
    assert chosen["tests"] == chosen["branch"] == ["tests/test_b.py::one"]


def _adopt(tmp_path: Path, planner: bool, outputs: str, collection: str = "success") -> dict:
    workspace = tmp_path / "workspace"
    (workspace / "scripts").mkdir(parents=True)
    if planner:
        (workspace / "scripts/ci_branch_plan.py").write_text("")
    (workspace / "README").write_text("")
    git = ["git", "-C", str(workspace), "-c", "user.name=t", "-c", "user.email=t@t"]
    subprocess.run([*git, "init", "-q"], check=True)
    subprocess.run([*git, "add", "."], check=True)
    subprocess.run([*git, "commit", "-qm", "dev"], check=True)
    subprocess.run([*git, "update-ref", "refs/remotes/origin/dev", "HEAD"], check=True)
    (workspace / "plan-outputs.txt").write_text(outputs)
    runner = tmp_path / "runner"
    runner.mkdir()
    output = tmp_path / "output"
    output.write_text("")
    env = {
        **os.environ,
        "RUNNER_TEMP": str(runner),
        "GITHUB_WORKSPACE": str(workspace),
        "GITHUB_OUTPUT": str(output),
        "COLLECTION": collection,
    }
    run = _step("unit-selected", "Adopt the reused tests from the protected grader")["run"]
    result = subprocess.run(["bash", "-e", "-c", run], cwd=workspace, env=env, capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr
    return dict(line.split("=", 1) for line in output.read_text().splitlines())


@pytest.mark.parametrize(
    ("outputs", "reused"),
    [("reused=0\nbranch=4\n", True), ("reused=3\nbranch=0\n", True), ("reused=0\nbranch=0\n", False), ("", False)],
)
def test_a_selected_leg_skips_tests_only_when_the_grader_planned_reuse(tmp_path, outputs, reused):
    adopted = _adopt(tmp_path, planner=True, outputs=outputs)
    assert adopted.get("graded") == "true"
    assert adopted.get("reused") == (str(tmp_path / "workspace/reused.json") if reused else None)


def test_a_selected_leg_runs_everything_when_the_grader_has_no_branch_planner(tmp_path):
    assert _adopt(tmp_path, planner=False, outputs="reused=3\nbranch=4\n") == {}


def test_a_selected_leg_waits_for_the_early_collection_before_it_runs():
    names = [step.get("name") for step in _jobs()["unit-selected"]["steps"]]
    wait = _step("unit-selected", "Wait for the early collection")
    assert wait["uses"] == "./.github/actions/await-download"
    assert wait["with"] == {"name": "early-collected-${{ matrix.python-version }}"}
    adopt = _step("unit-selected", "Adopt the reused tests from the protected grader")
    assert wait["if"] == adopt["if"]
    assert adopt["env"]["COLLECTION"] == f"${{{{ steps.{wait['id']}.outcome }}}}"
    assert (
        names.index("Wait for the early collection")
        < names.index("Adopt the reused tests from the protected grader")
        < names.index("Run the selected tests")
    )


def test_a_selected_leg_stops_waiting_for_the_collection_well_inside_its_timeout():
    wait = _step("unit-selected", "Wait for the early collection")
    run = _step("unit-selected", "Run the selected tests")
    assert wait["continue-on-error"] is True
    assert wait["timeout-minutes"] + run["timeout-minutes"] < _jobs()["unit-selected"]["timeout-minutes"]


@pytest.mark.parametrize("collection", ["failure", "cancelled", ""])
def test_a_selected_leg_whose_collection_never_landed_runs_everything_ungraded(tmp_path, collection):
    assert _adopt(tmp_path, planner=True, outputs="reused=3\nbranch=4\n", collection=collection) == {}


@pytest.mark.parametrize("reused", ["", "/w/reused.json"])
def test_the_leg_check_grades_only_the_legs_files_from_the_grader(tmp_path, reused):
    step = _step("unit-selected", "Check every selected test ran once or was reused")
    assert step["if"] == "steps.reuse.outputs.graded == 'true'"
    bin_dir, grader = tmp_path / "bin", tmp_path / "runner/grader"
    bin_dir.mkdir()
    grader.mkdir(parents=True)
    calls = tmp_path / "calls.json"
    (bin_dir / "python").write_text(
        f"#!{sys.executable}\nimport json, os, sys\n"
        f"open({str(calls)!r}, 'w').write(json.dumps([os.getcwd(), *sys.argv[1:]]))\n"
    )
    (bin_dir / "python").chmod(0o755)
    env = {
        **os.environ,
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "RUNNER_TEMP": str(tmp_path / "runner"),
        "GITHUB_WORKSPACE": "/w",
        "FILES": "tests/test_a.py tests/test_b.py",
        "REUSED": reused,
        "LEG": "3",
    }
    subprocess.run(["bash", "-e", "-c", step["run"]], cwd=tmp_path, env=env, check=True)
    cwd, *argv = json.loads(calls.read_text())
    assert Path(cwd).resolve() == grader.resolve()
    assert argv == [
        "-m",
        "tests.shard_check",
        "/w/durations.json",
        "--within",
        "tests/test_a.py",
        "tests/test_b.py",
        "--collected",
        "/w/collected.json",
        *(["--reused", reused] if reused else []),
        "--leg",
        "3",
    ]


def test_full_scope_shard_check_grades_and_summarizes_the_branch_plan():
    check = _step("shard-check", "Check every collected test ran in exactly one shard")
    assert "--reused branch-plan/reused-branch.json" in check["env"]["REUSED"]
    assert "--reused reused-plan/reused-tests.json" in check["env"]["REUSED"]
    summary = _step("shard-check", "Summarize reused, ran and flaky tests")
    assert summary["env"]["BRANCH_PLAN"] == (
        "${{ hashFiles('branch-plan/reused-branch.json') != '' && '--branch-plan branch-plan/reused-branch.json' || '' }}"
    )
    assert " $BRANCH_PLAN |" in summary["run"]


@pytest.mark.parametrize(
    ("values", "carried"),
    [
        ({}, True),
        ({"github.event_name": "merge_group"}, False),
        ({"github.event_name": "push"}, False),
        ({"github.event.pull_request.head.repo.full_name": "fork/r"}, False),
        ({"steps.source.outputs.test_only": "true"}, False),
    ],
)
def test_sonar_carries_the_branch_coverage_only_on_same_repo_pull_requests(values, carried):
    base = {**SAME_REPO, "needs.select.outputs.scope": "selected", "steps.source.outputs.test_only": "false"}
    names = [step.get("name") for step in _jobs()["sonar"]["steps"]]
    carry = _step("sonar", "Carry the pull request runs' coverage of the reused tests")
    mint = next(step for step in _jobs()["sonar"]["steps"] if step.get("id") == "app-token")
    assert mint["if"] == carry["if"]
    assert _evaluate(carry["if"], {**base, **values}) is carried
    assert names.index("Download shard coverage") < names.index(carry["name"]) < names.index("Merge shard coverage")
    assert carry["env"] == {
        "GH_TOKEN": "${{ steps.app-token.outputs.token }}",
        "GRADER": "${{ github.event.pull_request.base.sha }}",
    }
    assert 'cd "$RUNNER_TEMP/grader"' in carry["run"]
    assert carry["run"].index('cd "$RUNNER_TEMP/grader"') < carry["run"].index("python -m scripts.ci_branch_coverage")
    assert '--out "$GITHUB_WORKSPACE/.coverage-shards"' in carry["run"]


def test_combine_adds_every_carried_branch_database_that_holds_data(tmp_path):
    shards = tmp_path / ".coverage-shards"
    (shards / "coverage-3.12-1").mkdir(parents=True)
    (shards / "coverage-3.12-1/.coverage").write_text("x")
    for name, text in ((".coverage", "x"), (".coverage.host.pid7.a", "x"), (".coverage.empty", "")):
        (shards / "branch-5/1-2").mkdir(parents=True, exist_ok=True)
        (shards / "branch-5/1-2" / name).write_text(text)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    calls = tmp_path / "calls.txt"
    (bin_dir / "python").write_text(f'#!/usr/bin/env bash\necho "$*" >> {calls}\n')
    (bin_dir / "python").chmod(0o755)
    env = {**os.environ, "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}"}
    combine = ROOT / ".github/coverage/combine.sh"
    result = subprocess.run(
        ["bash", str(combine), "--downloaded", "1"], cwd=tmp_path, env=env, capture_output=True, text=True
    )
    assert result.returncode == 0, result.stdout + result.stderr
    flattened = [line.split()[1] for line in calls.read_text().splitlines() if line.endswith(tuple("0123456789"))]
    assert sorted(flattened) == [
        ".coverage-shards/branch-5/1-2/.coverage",
        ".coverage-shards/branch-5/1-2/.coverage.host.pid7.a",
        ".coverage-shards/coverage-3.12-1/.coverage",
    ]
