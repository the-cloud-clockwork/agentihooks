import os
import re
import signal
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

CASES = """
import os, time, pytest

def test_crash(tmp_path_factory):
    flag = tmp_path_factory.getbasetemp().parent.parent / "crashed"
    if not flag.exists():
        flag.write_text("1")
        os._exit(1)

@pytest.mark.parametrize("n", range(60))
def test_ok(n):
    time.sleep(0.3)
"""


def test_a_replacement_worker_keeps_the_live_workers_coverage_data(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    (project / "test_cases.py").write_text(CASES)
    (tmp_path / "base" / "b").mkdir(parents=True)
    command = [sys.executable, "-m", "pytest", "test_cases.py", "-n", "3", "--dist", "loadgroup", "-v"]
    command += ["-p", "no:cacheprovider", "-p", "tests.browser_coverage"]
    command += ["--cov=.", "--cov-report=", "--cov-context=test", f"--basetemp={tmp_path / 'base' / 'b' / 'b'}"]
    env = {key: value for key, value in os.environ.items() if not key.startswith(("COV_CORE_", "COVERAGE_", "PYTEST_"))}
    env["PYTHONPATH"] = str(ROOT)
    process = subprocess.Popen(
        command,
        cwd=project,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        start_new_session=True,
    )
    try:
        output = process.communicate(timeout=120)[0]
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGKILL)
        output = "timed out\n" + process.communicate()[0]
    assert "replacing crashed worker" in output, output
    assert not re.findall(r"^\[gw\d+\] .*\bERROR\b", output, re.M), output
    assert len(set(re.findall(r"^\[gw\d+\] .*\bPASSED (test_cases.py::test_ok\[\d+\])", output, re.M))) == 60, output
    assert re.findall(r"^\[gw\d+\] .*\bFAILED (\S+)", output, re.M) == ["test_cases.py::test_crash"], output
