import contextlib
import hashlib
import json
import os
import signal
import subprocess
import sys
import sysconfig
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from tests import branch_record, pass_record

ROOT = Path(__file__).resolve().parents[1]

OUTCOMES = """import os
from pathlib import Path

import pytest


def test_env():
    assert "CI_RESULTS" not in os.environ


def test_retried():
    marker = Path(__file__).with_name("retried")
    if not marker.exists():
        marker.write_text("")
        raise AssertionError("first attempt")


def test_skipped():
    pytest.skip("not here")


@pytest.fixture
def broken():
    yield
    raise RuntimeError("teardown")


def test_teardown_error(broken):
    pass


def test_fails():
    raise AssertionError("always")
"""


def _env(**extra: str) -> dict[str, str]:
    env = {key: value for key, value in os.environ.items() if not key.startswith(("COV_CORE_", "COVERAGE_", "PYTEST_"))}
    return {**env, "PYTHONPATH": str(ROOT), **extra}


def _results(folder: Path) -> list[dict]:
    return [
        {**json.loads(line), "file": path.stem}
        for path in folder.glob("*.jsonl")
        for line in path.read_text().splitlines()
    ]


@pytest.mark.parametrize("workers", ["0", "2"])
def test_only_each_final_attempt_is_recorded_and_ci_results_never_reaches_the_tests(tmp_path, workers):
    (tmp_path / "test_sample.py").write_text(OUTCOMES)
    results = tmp_path / "results"
    run = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "tests.pass_record", "-p", "no:cacheprovider"]
        + ["-n", workers, "--reruns", "1", "test_sample.py"],
        cwd=tmp_path,
        env=_env(CI_RESULTS=str(results)),
        capture_output=True,
        text=True,
    )
    assert run.returncode == 1, run.stdout + run.stderr
    rows = _results(results)
    assert {row["test"]: row["outcome"] for row in rows} == {
        "test_sample.py::test_env": "passed",
        "test_sample.py::test_retried": "flaky",
        "test_sample.py::test_skipped": "skipped",
        "test_sample.py::test_teardown_error": "failed",
        "test_sample.py::test_fails": "failed",
    }
    assert len(rows) == 5
    assert all(row["pid"] == int(row["file"]) for row in rows)


def test_the_results_folder_reaches_workers_through_their_input_and_leaves_the_environment(tmp_path, monkeypatch):
    folder = tmp_path / "results"
    monkeypatch.setenv("CI_RESULTS", str(folder))
    controller = SimpleNamespace(
        stash=pytest.Stash(), getoption=lambda name, default=None: "load", pluginmanager=Mock()
    )
    pass_record.pytest_configure(controller)
    assert "CI_RESULTS" not in os.environ
    controller.pluginmanager.register.assert_not_called()
    node = SimpleNamespace(config=controller, workerinput={})
    pass_record.pytest_configure_node(node)
    assert node.workerinput == {"ci_results": str(folder)}
    worker = SimpleNamespace(stash=pytest.Stash(), workerinput=node.workerinput, pluginmanager=Mock())
    pass_record.pytest_configure(worker)
    recorder, name = worker.pluginmanager.register.call_args.args
    assert name == "pass-record" and recorder.path == folder / f"{os.getpid()}.jsonl"


KILLED = {
    ".coveragerc": "[run]\nrelative_files = True\nsource = .\n",
    "sample.py": "def tidy():\n    return 1\n",
    "conftest.py": (
        "import time\nfrom pathlib import Path\n\n\n"
        "def pytest_runtest_logstart(nodeid, location):\n"
        "    if nodeid.endswith('::test_b'):\n"
        "        Path(__file__).with_name('hanging').write_text('')\n"
        "        time.sleep(300)\n"
    ),
    "test_one.py": (
        "import pytest\n\nimport sample\n\n\n"
        "@pytest.fixture\ndef tidied():\n    yield\n    sample.tidy()\n\n\n"
        "def test_a(tidied):\n    pass\n\n\n"
        "def test_b():\n    pass\n"
    ),
}


def test_a_killed_run_keeps_the_teardown_coverage_and_inputs_of_every_test_it_recorded(tmp_path):
    for name, text in KILLED.items():
        (tmp_path / name).write_text(text)
    inputs = tmp_path / "inputs"
    command = [sys.executable, "-m", "pytest", "-q", "-p", "tests.dep_inputs", "-p", "tests.pass_record"]
    command += ["-p", "no:cacheprovider", "-n", "2", "--dist", "loadfile", "--cov", "--cov-context=test"]
    env = _env(DEP_INPUTS_DIR=str(inputs), CI_RESULTS=str(tmp_path / "results"))
    with (tmp_path / "output.txt").open("w") as output:
        run = subprocess.Popen(
            [*command, "--cov-report=", "test_one.py"],
            cwd=tmp_path,
            env=env,
            stdout=output,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    try:
        deadline = time.monotonic() + 40
        while not (tmp_path / "hanging").exists() and run.poll() is None and time.monotonic() < deadline:
            time.sleep(0.05)
        assert (tmp_path / "hanging").exists(), (tmp_path / "output.txt").read_text()
    finally:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(run.pid, signal.SIGKILL)
        run.wait()
        name = hashlib.sha256(str(inputs / "children").encode()).hexdigest()[:16]
        (Path(sysconfig.get_path("purelib")) / f"agentihooks-dep-inputs-{name}.pth").unlink(missing_ok=True)
    assert not (tmp_path / "elapsed.json").exists()
    assert [row["test"] for row in _results(inputs / "finished")] == ["test_one.py::test_a"]
    out = tmp_path / "record.json"
    argv = ["--coverage-dir", str(tmp_path), "--inputs", str(inputs), "--results", str(tmp_path / "results")]
    argv += ["--flakes", str(tmp_path / "flakes.jsonl"), "--root", str(tmp_path), "--commit", "c1", "--head", "h1"]
    argv += ["--pull", "7", "--run", "11", "--attempt", "1", "--leg", "1", "--environment", "env", "--out", str(out)]
    assert branch_record.main(argv) == 0
    found = json.loads(out.read_text())
    assert found["passed"] == ["test_one.py::test_a"]
    manifest = found["manifest"]
    units = {manifest["units"][number] for number in manifest["tests"]["test_one.py::test_a"]["units"]}
    assert "sample.py::tidy" in units
