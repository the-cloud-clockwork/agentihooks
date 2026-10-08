import json
import os
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


def _check(tmp_path, *shards, suite=SUITE):
    tests = tmp_path / "suite"
    tests.mkdir(exist_ok=True)
    (tests / "test_suite.py").write_text(suite)
    paths = []
    for index, nodeids in enumerate(shards, 1):
        path = tmp_path / f"durations-3.12-{index}" / "durations.json"
        path.parent.mkdir()
        path.write_text(json.dumps(dict.fromkeys(nodeids, 0.1)))
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
    assert job["needs"] == ["unit"]
    assert "if" not in job
    assert job["strategy"]["matrix"]["python-version"] == jobs["unit"]["strategy"]["matrix"]["python-version"]
    assert "shard-check" in jobs["gate-required"]["needs"]
    lookup, *steps = job["steps"]
    assert lookup["id"] == "lookup"
    assert lookup["if"] == "github.event_name == 'push'"
    assert all(step["if"].startswith("steps.lookup.outputs.skip != 'true'") for step in steps)
    download = next(step for step in steps if step.get("uses", "").startswith("actions/download-artifact"))
    assert download["with"]["pattern"] == "durations-${{ matrix.python-version }}-*"
    check = steps[-1]
    assert "python -m tests.shard_check" in check["run"]
    assert download["with"]["path"] in check["run"]


@pytest.mark.parametrize(
    ("passed", "uploaded", "skip"),
    [
        (0, [], "false"),
        (1, [], "true"),
        (1, ["durations-3.12-1"], "false"),
        (1, ["durations-3.11-1", "durations-merged"], "true"),
        (None, [], "false"),
    ],
)
def test_check_skips_only_when_unit_skipped_a_passed_tree(tmp_path, passed, uploaded, skip):
    step = yaml.safe_load((_ROOT / ".github/workflows/test.yml").read_text())["jobs"]["shard-check"]["steps"][0]
    (tmp_path / "passed.json").write_text(json.dumps({"artifacts": [] if passed is None else _passed(passed)}))
    (tmp_path / "run.json").write_text(json.dumps({"artifacts": [{"name": name} for name in uploaded]}))
    gh = tmp_path / "gh"
    gh.write_text(
        "#!/usr/bin/env bash\n"
        f'[ "{passed is None}" = True ] && [[ "$2" == *tests-passed* ]] && exit 1\n'
        f'fixture="{tmp_path}/passed.json"; [[ "$2" == */runs/* ]] && fixture="{tmp_path}/run.json"\n'
        'while [ $# -gt 0 ]; do [ "$1" = --jq ] && f="$2"; shift; done\n'
        'jq -r "$f" "$fixture"\n'
    )
    gh.chmod(0o755)
    out = tmp_path / "out"
    env = {
        "PATH": f"{tmp_path}:{os.environ['PATH']}",
        "GITHUB_OUTPUT": str(out),
        "GITHUB_REPOSITORY": "o/r",
        "GITHUB_RUN_ID": "7",
        "TREE": "abc",
        "VERSION": "3.12",
    }
    subprocess.run(["bash", "-e", "-c", step["run"]], env=env, check=True)
    assert out.read_text() == f"skip={skip}\n"


def _passed(count: int) -> list[dict]:
    return [{"expired": False, "workflow_run": {"head_repository_id": 1, "repository_id": 1}}] * count
