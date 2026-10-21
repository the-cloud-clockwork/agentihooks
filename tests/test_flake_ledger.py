import json
import os
import subprocess
import sys
from collections import OrderedDict
from pathlib import Path
from types import SimpleNamespace

import pluggy
import pytest
import xdist.newhooks
import yaml
from xdist.scheduler import LoadScopeScheduling

from tests.flake_ledger import FlakeLedger, RefillingLoadGroupScheduling

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


def _run(tmp_path, *select, workers="0", probes=_PROBES, reruns="2", extra=()):
    (tmp_path / "test_probes.py").write_text(probes)
    ledger = tmp_path / "flakes.jsonl"
    targets = [f"test_probes.py::{name}" for name in select] or ["test_probes.py"]
    try:
        result = _pytest(tmp_path, ledger, targets, workers, reruns, extra)
    except subprocess.TimeoutExpired as hung:
        pytest.fail(f"probe pytest hung:\n{hung.stdout}\n{hung.stderr}")
    lines = ledger.read_text().splitlines() if ledger.exists() else []
    return result, [json.loads(line) for line in lines]


def _pytest(tmp_path, ledger, targets, workers, reruns, extra):
    return subprocess.run(
        [sys.executable, "-m", "pytest", *targets, "-p", "xdist", "-p", "rerunfailures", "-p", "tests.flake_ledger"]
        + ["-p", "no:cacheprovider", "-n", workers, "--reruns", reruns, "-q", *extra],
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
        timeout=45,
    )


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


_CRASHES = """
import os
from pathlib import Path

import pytest


def _crash_first(name, times):
    marker = Path(name)
    runs = int(marker.read_text()) + 1 if marker.exists() else 1
    marker.write_text(str(runs))
    if runs <= times:
        os._exit(1)


def test_crashes_once():
    _crash_first("crashed-once", 1)


def test_crashes_twice():
    _crash_first("crashed-twice", 2)


def test_crashes_always():
    _crash_first("crashed-always", 99)


@pytest.mark.parametrize("n", range(6))
def test_sibling(n):
    pass
"""


def _run_crash(tmp_path, *names, reruns="2"):
    return _run(tmp_path, *names, workers="2", probes=_CRASHES, reruns=reruns, extra=("--dist", "loadgroup"))


def test_a_worker_crash_that_passes_on_retry_is_green_and_reports_the_crash(tmp_path):
    result, records = _run_crash(tmp_path, "test_crashes_once")
    assert result.returncode == 0, result.stdout + result.stderr
    [record] = records
    assert record["test"] == "test_probes.py::test_crashes_once"
    assert record["outcome"] == "flaky"
    assert record["crashes"] == 1
    assert record["attempts"] == 2
    [error] = record["errors"]
    assert error.endswith("crashed while running 'test_probes.py::test_crashes_once'")


def test_a_worker_crash_that_repeats_on_retry_stays_red(tmp_path):
    result, records = _run_crash(tmp_path, "test_crashes_twice")
    assert result.returncode == 1, result.stdout + result.stderr
    assert "crashed while running 'test_probes.py::test_crashes_twice'" in result.stdout
    [record] = records
    assert record["test"] == "test_probes.py::test_crashes_twice"
    assert record["outcome"] == "failed"
    assert record["crashes"] == 2
    assert record["attempts"] == int((tmp_path / "crashed-twice").read_text()) == 3
    assert len(record["errors"]) == 2


def test_a_test_crashing_every_attempt_stops_after_its_reruns_and_every_other_test_runs(tmp_path):
    result, records = _run_crash(tmp_path, "test_crashes_always", "test_sibling")
    assert result.returncode == 1, result.stdout + result.stderr
    assert "6 passed" in result.stdout
    assert "maximum crashed workers" not in result.stdout
    assert "crashed while running 'test_probes.py::test_crashes_always'" in result.stdout
    assert (tmp_path / "crashed-always").read_text() == "3"
    [record] = records
    assert record["test"] == "test_probes.py::test_crashes_always"
    assert record["outcome"] == "failed"
    assert record["crashes"] == record["attempts"] == 3


def test_a_crash_past_the_worker_restart_limit_stays_failed(tmp_path):
    ledger = FlakeLedger(tmp_path / "flakes.jsonl")
    sched = LoadScopeScheduling.__new__(LoadScopeScheduling)
    sched.config = SimpleNamespace(
        option=SimpleNamespace(maxworkerrestart="2", numprocesses=1), getoption=lambda name, default=None: 2
    )
    outcomes = []
    for n in range(3):
        ledger.pytest_testnodedown(node=None, error="Not properly terminated")
        report = SimpleNamespace(failed=True, outcome="failed")
        ledger.pytest_handlecrashitem(crashitem=f"test_probes.py::test_{n}", report=report, sched=sched)
        outcomes.append(report.outcome)
    assert outcomes == ["rerun", "rerun", "failed"]


class _Worker:
    def __init__(self, events):
        self.shutting_down = False
        self.events = events

    def send_runtest_some(self, indices):
        self.events.extend(("sent", index) for index in indices)

    def shutdown(self):
        self.shutting_down = True


class _Recorder:
    def __init__(self, events):
        self.events = events

    @pytest.hookimpl
    def pytest_handlecrashitem(self, crashitem, report, sched):
        self.events.append(("recorded", crashitem))


def test_a_crashed_test_reaches_another_worker_only_after_its_crash_is_recorded(tmp_path):
    crashing, sibling = "test_probes.py::test_crashes_always", "test_probes.py::test_sibling"
    events = []
    crashed, alive = _Worker(events), _Worker(events)
    sched = RefillingLoadGroupScheduling.__new__(RefillingLoadGroupScheduling)
    sched.config = SimpleNamespace(option=SimpleNamespace(maxworkerrestart="2"), getoption={"reruns": 2}.get)
    sched.log = lambda *args: None
    sched.workqueue = OrderedDict()
    sched.registered_collections = {crashed: [crashing, sibling], alive: [crashing, sibling]}
    sched.assigned_work = {crashed: {crashing: {crashing: False}}, alive: {sibling: {sibling: True}}}
    hooks = pluggy.PluginManager("pytest")
    hooks.add_hookspecs(xdist.newhooks)
    hooks.register(_Recorder(events))
    hooks.register(FlakeLedger(tmp_path / "flakes.jsonl"))
    assert sched.remove_node(crashed) == crashing
    assert events == []
    report = SimpleNamespace(failed=True, outcome="failed")
    hooks.hook.pytest_handlecrashitem(crashitem=crashing, report=report, sched=sched)
    assert events == [("recorded", crashing), ("sent", 0)]
    assert report.outcome == "rerun"


def test_a_crashed_test_skips_a_replacement_worker_that_has_not_collected(tmp_path):
    crashing, sibling = "test_probes.py::test_crashes_always", "test_probes.py::test_sibling"
    events = []
    crashed, alive, joining = _Worker(events), _Worker(events), _Worker(events)
    sched = RefillingLoadGroupScheduling.__new__(RefillingLoadGroupScheduling)
    sched.config = SimpleNamespace(option=SimpleNamespace(maxworkerrestart="2"), getoption={"reruns": 2}.get)
    sched.log = lambda *args: None
    sched.workqueue = OrderedDict()
    sched.registered_collections = {crashed: [crashing, sibling], alive: [crashing, sibling]}
    sched.assigned_work = {crashed: {crashing: {crashing: False}}, joining: {}, alive: {sibling: {sibling: True}}}
    assert sched.remove_node(crashed) == crashing
    report = SimpleNamespace(failed=True, outcome="failed")
    FlakeLedger(tmp_path / "flakes.jsonl").pytest_handlecrashitem(crashitem=crashing, report=report, sched=sched)
    assert events == [("sent", 0)]
    assert not sched.workqueue
    sched.numnodes, sched.collection = 2, [crashing, sibling]
    sched.workqueue[sibling] = {sibling: False}
    sched.schedule()
    assert events == [("sent", 0), ("sent", 1)]
    assert not joining.shutting_down


def test_a_worker_crash_without_reruns_stays_red_and_is_not_run_again(tmp_path):
    result, records = _run_crash(tmp_path, "test_crashes_once", reruns="0")
    assert result.returncode == 1, result.stdout + result.stderr
    assert (tmp_path / "crashed-once").read_text() == "1"
    [record] = records
    assert record["outcome"] == "failed"
    assert record["crashes"] == 1
    assert record["attempts"] == 1


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
        {"test": "tests/test_c.py::test_crash", "run": "7", "attempts": 3, "outcome": "failed", "crashes": 2},
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
    assert warnings == [
        "::warning title=Flaky test::tests/test_a.py::test_once passed on attempt 2 in run 7",
        "::warning title=Worker crash::tests/test_c.py::test_crash crashed its worker 2 time(s) in run 7",
    ]
    assert summary.read_text().splitlines() == [
        "Tests retried in place: 3",
        "flaky after 2 attempts: tests/test_a.py::test_once",
        "failed after 3 attempts: tests/test_b.py::test_always",
        "failed after 3 attempts: tests/test_c.py::test_crash",
    ]
