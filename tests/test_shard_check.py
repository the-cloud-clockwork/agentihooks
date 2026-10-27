import json
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.unit
_ROOT = Path(__file__).resolve().parents[1]

SUITE = """
import pytest

def test_a():
    pass

def test_b():
    pass

@pytest.mark.parametrize("n", [1, 2])
def test_c(n):
    pass
"""
ALL = ["test_suite.py::test_a", "test_suite.py::test_b", "test_suite.py::test_c[1]", "test_suite.py::test_c[2]"]


def _check(tmp_path, *shards, suite=SUITE, hashes=None):
    tests = tmp_path / "suite"
    tests.mkdir(exist_ok=True)
    (tests / "pytest.ini").write_text("[pytest]\n")
    (tests / "test_suite.py").write_text(suite)
    paths = []
    for index, nodeids in enumerate(shards, 1):
        path = tmp_path / f"durations-3.12-{index}" / "durations.json"
        path.parent.mkdir()
        path.write_text(json.dumps(dict.fromkeys(nodeids, 0.1)))
        digest = "a" * 64 if hashes is None else hashes[index - 1]
        if digest is not None:
            (path.parent / "durations.sha256").write_text(f"{digest}\n")
        paths.append(str(path))
    return subprocess.run(
        [sys.executable, "-m", "tests.shard_check", "--tests", str(tests), *paths],
        cwd=_ROOT,
        capture_output=True,
        text=True,
    )


def test_every_test_in_exactly_one_shard_passes(tmp_path):
    result = _check(tmp_path, ALL[:2], ALL[2:])
    assert result.returncode == 0, result.stdout + result.stderr
    assert "4 collected tests, 0 ran zero times or more than once" in result.stdout


def test_a_dropped_test_is_red_and_named(tmp_path):
    result = _check(tmp_path, ALL[:1], ALL[2:])
    assert result.returncode == 1
    assert "test_suite.py::test_b ran 0 times" in result.stdout
    assert "::error::" in result.stdout


def test_a_test_run_in_two_shards_is_red_and_named(tmp_path):
    result = _check(tmp_path, ALL[:3], ALL[2:])
    assert result.returncode == 1
    assert "test_suite.py::test_c[1] ran 2 times" in result.stdout
    assert "1 ran zero times or more than once" in result.stdout


def test_xdist_group_suffix_names_the_same_test(tmp_path):
    result = _check(tmp_path, [f"{ALL[0]}@serial", *ALL[1:2]], ALL[2:])
    assert result.returncode == 0, result.stdout + result.stderr


def test_shards_split_on_different_stored_durations_are_red_and_named(tmp_path):
    result = _check(tmp_path, ALL[:2], ALL[2:], hashes=["a" * 64, "b" * 64])
    assert result.returncode == 1
    assert "::error::Shards split on different stored durations" in result.stdout
    assert f"durations-3.12-2: {'b' * 64}" in result.stdout


def test_a_shard_that_recorded_no_durations_hash_is_red_and_named(tmp_path):
    result = _check(tmp_path, ALL[:2], ALL[2:], hashes=["a" * 64, None])
    assert result.returncode == 1
    assert "::error::Shards split on different stored durations" in result.stdout
    assert "durations-3.12-2: none" in result.stdout


def test_a_collection_error_is_red(tmp_path):
    result = _check(tmp_path, ALL, suite=SUITE + "\nimport not_a_module_anywhere\n")
    assert result.returncode == 1
    assert "::error::" in result.stdout


def test_an_empty_collection_is_red(tmp_path):
    result = _check(tmp_path, [], suite="")
    assert result.returncode == 1
    assert "::error::" in result.stdout


def test_a_shard_without_durations_is_red_and_named(tmp_path):
    (tmp_path / "suite").mkdir()
    (tmp_path / "suite" / "test_suite.py").write_text(SUITE)
    missing = tmp_path / "shard-durations" / "*" / "durations.json"
    result = subprocess.run(
        [sys.executable, "-m", "tests.shard_check", "--tests", str(tmp_path / "suite"), str(missing)],
        cwd=_ROOT,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 1
    assert f"::error::No shard durations at {missing}" in result.stdout


def test_shard_check_grades_every_unit_shard_before_the_required_gate():
    jobs = yaml.safe_load((_ROOT / ".github/workflows/test.yml").read_text())["jobs"]
    job = jobs["shard-check"]
    assert job["needs"] in (
        ["unit", "unit-rerun", "select", "reuse", "admission"],
        ["split", "select", "reuse", "admission"],
    )
    assert job["if"] in (
        "${{ !cancelled() && (github.event_name != 'merge_group' || needs.reuse.outputs.reused != 'true') }}",
        "${{ !cancelled() && (github.event_name != 'workflow_dispatch' || needs.admission.result == 'success') && (github.event_name != 'merge_group' || needs.reuse.outputs.reused != 'true') }}",
        "${{ !cancelled() && (github.event_name != 'workflow_dispatch' || needs.admission.result == 'success') && (github.event_name != 'merge_group' || needs.reuse.outputs.reused != 'true') && (github.event_name != 'pull_request' || needs.select.outputs.scope == 'full') }}",
        "${{ !cancelled() && (github.event_name != 'workflow_dispatch' || needs.admission.result == 'success') && (github.event_name != 'pull_request' || needs.select.outputs.scope == 'full') }}",
    )
    assert (
        job["strategy"]["matrix"]["python-version"] == jobs["unit"]["strategy"]["matrix"]["python-version"] == ["3.12"]
    )
    assert "shard-check" in jobs["gate-required"]["needs"]
    steps = job["steps"]
    assert all("steps.lookup" not in step.get("if", "") for step in steps)
    download = next(step for step in steps if step.get("uses", "").startswith("actions/download-artifact"))
    if job["needs"][0] == "split":
        assert download["with"] == {
            "pattern": "{collected,reused-tests}-${{ matrix.python-version }}",
            "path": "shard-data",
        }
        prefetch = next(step for step in steps if step.get("name") == "Wait for every planned shard")
        assert prefetch["env"]["GH_TOKEN"] == "${{ steps.app-token.outputs.token }}"
        assert "python -m scripts.ci_gate_shards" in prefetch["run"]
        assert steps.index(prefetch) < next(
            i
            for i, step in enumerate(steps)
            if step.get("run", "").startswith(("python -m tests.shard_check", "python3 -m tests.shard_check"))
        )
    else:
        assert download["with"]["pattern"] in (
            "durations-${{ matrix.python-version }}-*",
            "{durations,flakes,collected,reused-tests}-${{ matrix.python-version }}{,-*}",
        )
    check = next(
        step
        for step in steps
        if step.get("run", "").startswith(("python -m tests.shard_check", "python3 -m tests.shard_check"))
    )
    assert "--collected collected-data/collected.json" in check["run"]
    assert steps.index(download) < steps.index(check)


def _plan(tmp_path, tests):
    path = tmp_path / "reused-tests.json"
    path.write_text(json.dumps({"run": "7", "tests": tests}))
    return path


def _check_reused(tmp_path, plan, *shards):
    collected = tmp_path / "collected.json"
    collected.write_text(json.dumps(ALL))
    paths = []
    for index, nodeids in enumerate(shards, 1):
        path = tmp_path / f"durations-3.12-{index}" / "durations.json"
        path.parent.mkdir()
        path.write_text(json.dumps(dict.fromkeys(nodeids, 0.1)))
        (path.parent / "durations.sha256").write_text("a" * 64 + "\n")
        paths.append(str(path))
    return subprocess.run(
        [sys.executable, "-m", "tests.shard_check", *paths, "--collected", str(collected), "--reused", str(plan)],
        cwd=_ROOT,
        capture_output=True,
        text=True,
    )


def test_reused_tests_run_nowhere_and_the_rest_once(tmp_path):
    plan = _plan(tmp_path, [ALL[0], ALL[2], "test_gone.py::test_x"])
    result = _check_reused(tmp_path, plan, [ALL[1]], [ALL[3]])
    assert result.returncode == 0, result.stdout + result.stderr
    assert "4 collected tests, 0 ran zero times or more than once, 2 reused" in result.stdout


def test_a_reused_test_that_ran_is_red(tmp_path):
    plan = _plan(tmp_path, [ALL[0]])
    result = _check_reused(tmp_path, plan, ALL[:2], ALL[2:])
    assert result.returncode == 1
    assert f"{ALL[0]} ran 1 times though reused" in result.stdout


def test_a_test_the_plan_does_not_reuse_must_still_run(tmp_path):
    plan = _plan(tmp_path, [ALL[0]])
    result = _check_reused(tmp_path, plan, [ALL[1]], [ALL[2]])
    assert result.returncode == 1
    assert f"{ALL[3]} ran 0 times" in result.stdout


def test_an_unreadable_plan_is_red(tmp_path):
    plan = tmp_path / "reused-tests.json"
    plan.write_text("{}")
    result = _check_reused(tmp_path, plan, ALL)
    assert result.returncode == 1
    assert "::error::Cannot read the test plan" in result.stdout


def test_supplied_collection_is_verified_without_installed_dependencies(tmp_path):
    collected = tmp_path / "collected.json"
    collected.write_text(json.dumps(["suite::a", "suite::b"]))
    durations = tmp_path / "durations.json"
    durations.write_text(json.dumps({"suite::a": 0.1, "suite::b": 0.1}))
    (tmp_path / "durations.sha256").write_text("a" * 64)
    result = subprocess.run(
        [sys.executable, "-S", "-m", "tests.shard_check", str(durations), "--collected", str(collected)],
        cwd=_ROOT,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "2 collected tests, 0 ran zero times or more than once" in result.stdout


LEG = [
    "tests/test_a.py::one",
    "tests/test_a.py::two",
    "tests/test_a.py::three",
    "tests/test_b.py::four",
    "tests/test_b.py::five",
    "tests/test_b.py::six",
]


def _branch_plan(tmp_path, tests):
    path = tmp_path / "reused-branch.json"
    run = {"run": 5, "attempt": 1, "commit": "c" * 40, "head": "d" * 40, "tests": tests}
    path.write_text(json.dumps({"version": 1, "runs": [run], "tests": tests}))
    return path


def _grade(tmp_path, shards, *args, collected=ALL, hashed=True):
    collection = tmp_path / "collected.json"
    collection.write_text(json.dumps(collected))
    paths = []
    for index, nodeids in enumerate(shards, 1):
        path = tmp_path / f"durations-3.12-{index}" / "durations.json"
        path.parent.mkdir()
        path.write_text(json.dumps(dict.fromkeys(nodeids, 0.1)))
        if hashed:
            (path.parent / "durations.sha256").write_text("a" * 64 + "\n")
        paths.append(str(path))
    return subprocess.run(
        [sys.executable, "-m", "tests.shard_check", *paths, "--collected", str(collection), *args],
        cwd=_ROOT,
        capture_output=True,
        text=True,
    )


def test_a_single_dev_plan_prints_exactly_the_line_it_printed_before(tmp_path):
    plan = _plan(tmp_path, [ALL[0], ALL[2]])
    result = _grade(tmp_path, [[ALL[1]], [ALL[3]]], "--reused", str(plan))
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout == "4 collected tests, 0 ran zero times or more than once, 2 reused\n"


def test_dev_and_pull_request_plans_together_skip_their_tests_and_the_rest_run_once(tmp_path):
    dev, branch = _plan(tmp_path, [ALL[0]]), _branch_plan(tmp_path, [ALL[2]])
    result = _grade(tmp_path, [[ALL[1]], [ALL[3]]], "--reused", str(dev), "--reused", str(branch))
    assert result.returncode == 0, result.stdout + result.stderr
    assert "4 collected tests, 0 ran zero times or more than once, 2 reused" in result.stdout


def test_a_test_reused_from_a_pull_request_run_that_ran_is_red(tmp_path):
    dev, branch = _plan(tmp_path, [ALL[0]]), _branch_plan(tmp_path, [ALL[2]])
    result = _grade(tmp_path, [ALL[1:2], ALL[2:]], "--reused", str(dev), "--reused", str(branch))
    assert result.returncode == 1
    assert f"{ALL[2]} ran 1 times though reused" in result.stdout


def test_a_test_neither_plan_reuses_must_still_run(tmp_path):
    dev, branch = _plan(tmp_path, [ALL[0]]), _branch_plan(tmp_path, [ALL[2]])
    result = _grade(tmp_path, [[ALL[1]]], "--reused", str(dev), "--reused", str(branch))
    assert result.returncode == 1
    assert f"{ALL[3]} ran 0 times" in result.stdout


def test_one_leg_is_graded_against_its_own_files_and_counts_both_reuse_sources(tmp_path):
    dev, branch = _plan(tmp_path, [LEG[0], LEG[3]]), _branch_plan(tmp_path, [LEG[1], LEG[4]])
    args = ["--reused", str(dev), "--reused", str(branch), "--within", "tests/test_a.py", "--leg", "3"]
    result = _grade(tmp_path, [[LEG[2]]], *args, collected=LEG, hashed=False)
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.splitlines() == [
        "3 collected tests, 0 ran zero times or more than once, 2 reused",
        "Leg 3: ran 1, reused 1 from dev, 1 from pull request runs",
    ]


def test_a_leg_is_red_for_its_own_test_that_did_not_run_and_silent_on_other_legs(tmp_path):
    dev, branch = _plan(tmp_path, [LEG[0]]), _branch_plan(tmp_path, [LEG[1]])
    args = ["--reused", str(dev), "--reused", str(branch), "--within", "tests/test_a.py", "--leg", "3"]
    result = _grade(tmp_path, [[]], *args, collected=LEG, hashed=False)
    assert result.returncode == 1
    assert f"{LEG[2]} ran 0 times" in result.stdout
    assert "test_b.py" not in result.stdout


def test_a_leg_whose_files_hold_no_collected_test_is_red(tmp_path):
    result = _grade(tmp_path, [[LEG[0]]], "--within", "tests/test_gone.py", collected=LEG, hashed=False)
    assert result.returncode == 1
    assert "::error::No collected test is in tests/test_gone.py" in result.stdout


def test_a_deselect_output_counts_its_branch_tests_as_reused_from_pull_request_runs(tmp_path):
    deselected = tmp_path / "reused.json"
    deselected.write_text(json.dumps({"tests": [LEG[0], LEG[1]], "files": [], "branch": [LEG[1]]}))
    args = ["--reused", str(deselected), "--within", "tests/test_a.py", "--leg", "2"]
    result = _grade(tmp_path, [[LEG[2]]], *args, collected=LEG, hashed=False)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "Leg 2: ran 1, reused 1 from dev, 1 from pull request runs" in result.stdout


def test_an_unreadable_pull_request_plan_is_red_and_named(tmp_path):
    dev, branch = _plan(tmp_path, [ALL[0]]), tmp_path / "reused-branch.json"
    branch.write_text(json.dumps({"runs": []}))
    result = _grade(tmp_path, [ALL[1:]], "--reused", str(dev), "--reused", str(branch))
    assert result.returncode == 1
    assert f"::error::Cannot read the test plan at {branch}" in result.stdout
