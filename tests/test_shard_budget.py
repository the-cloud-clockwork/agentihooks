import json
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.unit
_ROOT = Path(__file__).resolve().parents[1]


def _budget(tmp_path, *shards, budget=None):
    paths = []
    for index, seconds in enumerate(shards, 1):
        path = tmp_path / f"durations-3.12-{index}" / "durations.json"
        path.parent.mkdir()
        path.write_text(json.dumps({f"test_suite.py::test_{n}": s for n, s in enumerate(seconds)}))
        paths.append(str(path))
    flags = ["--budget", str(budget)] if budget is not None else []
    return subprocess.run(
        [sys.executable, "-m", "tests.shard_budget", *flags, *paths],
        cwd=_ROOT,
        capture_output=True,
        text=True,
    )


def test_shards_inside_the_budget_pass_and_every_shard_is_reported(tmp_path):
    result = _budget(tmp_path, [300.0, 200.0], [100.0], [450.0, 449.0])
    assert result.returncode == 0, result.stdout + result.stderr
    lines = result.stdout.splitlines()
    assert lines[0] == "3 shards, slowest 899.0 s of test time against a budget of 900 s"
    assert [line.split()[0] for line in lines[1:]] == ["durations-3.12-3", "durations-3.12-1", "durations-3.12-2"]


def test_a_shard_over_the_fifteen_minute_default_is_red_and_named(tmp_path):
    result = _budget(tmp_path, [100.0], [450.0, 451.0])
    assert result.returncode == 1
    assert "durations-3.12-2 901.0 s over budget" in result.stdout
    assert "::error::1 of 2 shards passed the 900 s budget" in result.stdout


def test_a_shard_exactly_at_the_budget_passes(tmp_path):
    result = _budget(tmp_path, [450.0, 450.0])
    assert result.returncode == 0, result.stdout + result.stderr
    assert "durations-3.12-1 900.0 s" in result.stdout.splitlines()


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


def test_a_missing_shard_file_is_red(tmp_path):
    result = subprocess.run(
        [sys.executable, "-m", "tests.shard_budget", str(tmp_path / "durations-3.12-1" / "durations.json")],
        cwd=_ROOT,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 1
    assert "::error::No shard durations at" in result.stdout


def test_the_shard_check_job_grades_the_budget_on_the_stored_durations():
    steps = yaml.safe_load((_ROOT / ".github/workflows/test.yml").read_text())["jobs"]["shard-check"]["steps"]
    step = next(step for step in steps if step.get("run", "").startswith("python -m tests.shard_budget"))
    assert step["run"] == "python -m tests.shard_budget shard-durations/*/durations.json"
    assert step["if"] == "steps.lookup.outputs.skip != 'true'"
