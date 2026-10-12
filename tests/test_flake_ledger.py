import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

pytestmark = pytest.mark.unit

_ROOT = Path(__file__).parent.parent

_PROBES = """
import pytest

CALLS = {"once": 0}


def test_passes():
    pass


def test_fails_once():
    CALLS["once"] += 1
    assert CALLS["once"] > 1, "first attempt fails"


def test_always_fails():
    assert False, "every attempt fails"


@pytest.fixture
def broken():
    raise RuntimeError("setup breaks every attempt")


def test_setup_always_fails(broken):
    pass


@pytest.fixture
def teardown_breaks_once():
    yield
    CALLS["teardown"] = CALLS.get("teardown", 0) + 1
    if CALLS["teardown"] == 1:
        raise RuntimeError("teardown breaks the first attempt")


def test_teardown_fails_once(teardown_breaks_once):
    pass


@pytest.fixture
def teardown_breaks():
    yield
    raise RuntimeError("teardown breaks every attempt")


def test_teardown_always_fails(teardown_breaks):
    pass
"""


def _run(tmp_path, *select, workers="0"):
    (tmp_path / "test_probes.py").write_text(_PROBES)
    ledger = tmp_path / "flakes.jsonl"
    targets = [f"test_probes.py::{name}" for name in select] or ["test_probes.py"]
    result = subprocess.run(
        [sys.executable, "-m", "pytest", *targets, "-p", "xdist", "-p", "rerunfailures", "-p", "tests.flake_ledger"]
        + ["-p", "no:cacheprovider", "-n", workers, "--reruns", "2", "-q"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
        env={
            **os.environ,
            "PYTHONPATH": str(_ROOT),
            "PYTEST_ADDOPTS": "",
            "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
            "CI_FLAKES": str(ledger),
            "GITHUB_RUN_ID": "4242",
            "GITHUB_RUN_ATTEMPT": "1",
        },
    )
    lines = ledger.read_text().splitlines() if ledger.exists() else []
    return result, [json.loads(line) for line in lines]


@pytest.mark.parametrize("workers", ["0", "2"])
def test_a_test_that_passes_on_retry_passes_and_is_recorded_as_a_flake(tmp_path, workers):
    result, records = _run(tmp_path, "test_passes", "test_fails_once", workers=workers)
    assert result.returncode == 0, result.stdout + result.stderr
    assert records == [
        {
            "test": "test_probes.py::test_fails_once",
            "run": "4242",
            "run_attempt": "1",
            "attempts": 2,
            "outcome": "flaky",
            "errors": ["AssertionError: first attempt fails"],
        }
    ]


@pytest.mark.parametrize("workers", ["0", "2"])
def test_a_test_failing_every_attempt_fails_after_three_attempts(tmp_path, workers):
    result, records = _run(tmp_path, workers=workers)
    assert result.returncode == 1, result.stdout + result.stderr
    by_test = {record["test"]: record for record in records}
    assert set(by_test) == {
        "test_probes.py::test_fails_once",
        "test_probes.py::test_always_fails",
        "test_probes.py::test_setup_always_fails",
        "test_probes.py::test_teardown_fails_once",
        "test_probes.py::test_teardown_always_fails",
    }
    for name in ("test_fails_once", "test_teardown_fails_once"):
        assert by_test[f"test_probes.py::{name}"]["outcome"] == "flaky"
        assert by_test[f"test_probes.py::{name}"]["attempts"] == 2
    assert by_test["test_probes.py::test_teardown_fails_once"]["errors"] == [
        "RuntimeError: teardown breaks the first attempt"
    ]
    for name in ("test_always_fails", "test_setup_always_fails", "test_teardown_always_fails"):
        record = by_test[f"test_probes.py::{name}"]
        assert record["outcome"] == "failed"
        assert record["attempts"] == 3
        assert len(record["errors"]) == 3
    assert by_test["test_probes.py::test_always_fails"]["errors"] == ["AssertionError: every attempt fails"] * 3


def test_a_run_without_retries_writes_no_ledger(tmp_path):
    result, records = _run(tmp_path, "test_passes")
    assert result.returncode == 0, result.stdout + result.stderr
    assert records == []
    assert not (tmp_path / "flakes.jsonl").exists()


def test_unit_shards_retry_on_the_command_line_and_upload_the_ledger():
    workflow = yaml.safe_load((_ROOT / ".github/workflows/test.yml").read_text())
    for scope in (workflow, workflow["jobs"]["unit"]):
        assert "reruns" not in str(scope.get("env", {}))
    steps = workflow["jobs"]["unit"]["steps"]
    names = [step.get("name") for step in steps]
    run = steps[names.index("Run tests")]
    assert "-p tests.flake_ledger --reruns 2 " in run["run"]
    assert "reruns" not in run["env"]["PYTEST_ADDOPTS"]
    assert run["env"]["CI_FLAKES"] == "${{ github.workspace }}/flakes.jsonl"
    record = steps[names.index("Record flaky tests")]
    upload = steps[names.index("Upload the flake report")]
    assert record["if"] == upload["if"] == "${{ !cancelled() }}"
    assert names.index("Run tests") < names.index("Record flaky tests") < names.index("Upload the flake report")
    assert upload["with"]["name"] == "flakes-${{ matrix.python-version }}-${{ matrix.shard }}"
    assert upload["with"]["path"] == "flakes.jsonl"
    assert upload["with"]["if-no-files-found"] == "error"


def test_every_absorbed_flake_raises_a_warning_on_the_run(tmp_path):
    steps = yaml.safe_load((_ROOT / ".github/workflows/test.yml").read_text())["jobs"]["unit"]["steps"]
    record = next(step for step in steps if step.get("name") == "Record flaky tests")
    lines = [
        {"test": "tests/test_a.py::test_once", "run": "7", "attempts": 2, "outcome": "flaky", "errors": ["E"]},
        {"test": "tests/test_b.py::test_always", "run": "7", "attempts": 3, "outcome": "failed", "errors": ["E"] * 3},
    ]
    (tmp_path / "flakes.jsonl").write_text("".join(json.dumps(line) + "\n" for line in lines))
    summary = tmp_path / "summary.md"
    result = subprocess.run(
        ["bash", "-e", "-c", record["run"]],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, "GITHUB_STEP_SUMMARY": str(summary)},
    )
    assert result.returncode == 0, result.stderr
    warnings = [line for line in result.stdout.splitlines() if line.startswith("::warning")]
    assert warnings == ["::warning title=Flaky test::tests/test_a.py::test_once passed on attempt 2 in run 7"]
    assert summary.read_text().splitlines() == [
        "Tests retried in place: 2",
        "flaky after 2 attempts: tests/test_a.py::test_once",
        "failed after 3 attempts: tests/test_b.py::test_always",
    ]
