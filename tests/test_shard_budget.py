import json
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.unit
_ROOT = Path(__file__).resolve().parents[1]


def _budget(tmp_path, *shards, budget=None, report=False):
    paths = []
    for index, seconds in enumerate(shards, 1):
        path = tmp_path / f"durations-3.12-{index}" / "durations.json"
        path.parent.mkdir()
        path.write_text(json.dumps({f"test_suite.py::test_{n}": s for n, s in enumerate(seconds)}))
        paths.append(str(path))
    flags = (["--budget", str(budget)] if budget is not None else []) + (["--report"] if report else [])
    return subprocess.run(
        [sys.executable, "-m", "tests.shard_budget", *flags, *paths],
        cwd=_ROOT,
        capture_output=True,
        text=True,
    )


def test_shards_inside_the_budget_pass_and_every_shard_is_reported(tmp_path):
    result = _budget(tmp_path, [300.0, 200.0], [100.0], [360.0, 359.0])
    assert result.returncode == 0, result.stdout + result.stderr
    lines = result.stdout.splitlines()
    assert lines[0] == "3 shards, slowest 719.0 s of test time against the 12 minute Run tests deadline of 720 s"
    assert [line.split()[0] for line in lines[1:]] == ["durations-3.12-3", "durations-3.12-1", "durations-3.12-2"]


def test_a_shard_over_the_twelve_minute_default_is_red_and_named(tmp_path):
    result = _budget(tmp_path, [100.0], [360.0, 361.0])
    assert result.returncode == 1
    assert "durations-3.12-2 721.0 s over budget" in result.stdout
    assert "::error::1 of 2 shards passed the 720 s budget" in result.stdout


def test_a_report_names_the_shard_over_budget_without_failing(tmp_path):
    result = _budget(tmp_path, [100.0], [360.0, 361.0], report=True)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "durations-3.12-2 721.0 s over budget" in result.stdout
    assert "::warning::1 of 2 shards passed the 720 s budget" in result.stdout
    assert "::error::" not in result.stdout


def test_a_report_still_fails_when_a_shard_cannot_be_read(tmp_path):
    result = subprocess.run(
        [sys.executable, "-m", "tests.shard_budget", "--report", str(tmp_path / "missing" / "durations.json")],
        cwd=_ROOT,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 1
    assert "::error::No shard durations" in result.stdout


def test_a_shard_exactly_at_the_budget_passes(tmp_path):
    result = _budget(tmp_path, [360.0, 360.0])
    assert result.returncode == 0, result.stdout + result.stderr
    assert "durations-3.12-1 720.0 s" in result.stdout.splitlines()


def test_shards_sharing_a_folder_name_are_each_graded(tmp_path):
    paths = []
    for side, seconds in (("a", 100.0), ("b", 901.0)):
        path = tmp_path / side / "durations-3.12-1" / "durations.json"
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({"test_suite.py::test_0": seconds}))
        paths.append(str(path))
    result = subprocess.run(
        [sys.executable, "-m", "tests.shard_budget", *paths], cwd=_ROOT, capture_output=True, text=True
    )
    assert result.returncode == 1
    assert result.stdout.startswith("2 shards, slowest 901.0 s")


def test_the_budget_flag_moves_the_limit(tmp_path):
    result = _budget(tmp_path, [61.0], [59.0], budget=60)
    assert result.returncode == 1
    assert "::error::1 of 2 shards passed the 60 s budget" in result.stdout
    assert result.stdout.splitlines()[0].endswith("against a budget of 60 s")


def test_a_missing_shard_file_is_red(tmp_path):
    result = subprocess.run(
        [sys.executable, "-m", "tests.shard_budget", str(tmp_path / "durations-3.12-1" / "durations.json")],
        cwd=_ROOT,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 1
    assert "::error::No shard durations at" in result.stdout


def _elapsed_shard(tmp_path, index, summed, elapsed):
    path = tmp_path / f"durations-3.12-{index}" / "durations.json"
    path.parent.mkdir()
    path.write_text(json.dumps({"test_suite.py::test_0": summed}))
    if elapsed is not None:
        (path.parent / "elapsed.json").write_text(json.dumps({"seconds": elapsed}))
    return str(path)


def test_the_shard_budget_transition_accepts_summed_or_elapsed_time(tmp_path):
    paths = [_elapsed_shard(tmp_path, 1, 1081.5, 396), _elapsed_shard(tmp_path, 2, 215.0, None)]
    result = subprocess.run(
        [sys.executable, "-m", "tests.shard_budget", *paths], cwd=_ROOT, capture_output=True, text=True
    )
    assert (result.returncode, result.stdout.splitlines()[1:3]) in (
        (0, ["durations-3.12-1 396.0 s", "durations-3.12-2 215.0 s"]),
        (1, ["durations-3.12-1 1081.5 s over budget", "durations-3.12-2 215.0 s"]),
    ), result.stdout + result.stderr


def test_the_shard_budget_transition_accepts_either_over_budget_result(tmp_path):
    path = _elapsed_shard(tmp_path, 1, 10.0, 901)
    result = subprocess.run(
        [sys.executable, "-m", "tests.shard_budget", path], cwd=_ROOT, capture_output=True, text=True
    )
    assert (result.returncode, result.stdout.splitlines()[1]) in (
        (0, "durations-3.12-1 10.0 s"),
        (1, "durations-3.12-1 901.0 s over budget"),
    ), result.stdout + result.stderr


def test_a_shard_is_graded_on_its_recorded_elapsed_time_over_its_summed_test_time(tmp_path):
    paths = [_elapsed_shard(tmp_path, 1, 1081.5, 396), _elapsed_shard(tmp_path, 2, 215.0, None)]
    result = subprocess.run(
        [sys.executable, "-m", "tests.shard_budget", *paths], cwd=_ROOT, capture_output=True, text=True
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.splitlines()[1:] == ["durations-3.12-1 396.0 s", "durations-3.12-2 215.0 s"]


def test_an_elapsed_time_over_the_budget_is_red(tmp_path):
    path = _elapsed_shard(tmp_path, 1, 10.0, 901)
    result = subprocess.run(
        [sys.executable, "-m", "tests.shard_budget", path], cwd=_ROOT, capture_output=True, text=True
    )
    assert result.returncode == 1
    assert "durations-3.12-1 901.0 s over budget" in result.stdout


def test_the_shard_check_job_grades_the_budget_on_the_stored_durations():
    steps = yaml.safe_load((_ROOT / ".github/workflows/test.yml").read_text())["jobs"]["shard-check"]["steps"]
    commands = ("python -m tests.shard_budget", "python3 -m tests.shard_budget")
    step = next(step for step in steps if step.get("run", "").startswith(commands))
    assert step["run"] in (f"{command} $REPORT shard-durations/*/durations.json" for command in commands)
    assert step["if"] == "github.event_name != 'merge_group'"


def test_the_default_budget_is_the_run_tests_step_deadline(tmp_path):
    minutes = _step_minutes("unit", "Run tests")
    result = _budget(tmp_path, [100.0])
    assert result.stdout.splitlines()[0] == (
        f"1 shards, slowest 100.0 s of test time against the {minutes} minute Run tests deadline of {minutes * 60} s"
    )


def _step_minutes(job, name):
    steps = yaml.safe_load((_ROOT / ".github/workflows/test.yml").read_text())["jobs"][job]["steps"]
    return next(step for step in steps if step.get("name") == name)["timeout-minutes"]


def test_a_selected_leg_is_graded_against_the_selected_tests_step_deadline(tmp_path):
    seconds = _step_minutes("unit-selected", "Run the selected tests") * 60
    path = tmp_path / "durations-3.12-1" / "durations.json"
    path.parent.mkdir()
    path.write_text(json.dumps({"test_suite.py::test_0": 10.0}))
    (path.parent / "elapsed.json").write_text(json.dumps({"seconds": seconds + 1, "job": "unit-selected"}))
    result = subprocess.run(
        [sys.executable, "-m", "tests.shard_budget", str(path)], cwd=_ROOT, capture_output=True, text=True
    )
    assert result.returncode == 1
    assert result.stdout.splitlines()[0] == (
        f"1 shards, slowest {seconds + 1:.1f} s of test time against the {seconds // 60} minute "
        f"Run the selected tests deadline of {seconds} s"
    )
    assert f"::error::1 of 1 shards passed the {seconds} s budget." in result.stdout
