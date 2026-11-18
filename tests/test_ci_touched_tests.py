import subprocess
import sys

import pytest


@pytest.mark.wall_clock
@pytest.mark.parametrize("job", ["unit", "unit-selected"])
def test_a_hung_worker_fails_before_the_step_timeout(tmp_path, monkeypatch, job):
    import shlex
    from pathlib import Path

    import yaml

    root = Path(__file__).resolve().parents[1]
    workflow = yaml.safe_load((root / ".github/workflows/test.yml").read_text())
    step_name = "Run tests" if job == "unit" else "Run the selected tests"
    command = next(step["run"] for step in workflow["jobs"][job]["steps"] if step.get("name") == step_name)
    options = shlex.split(command)
    assert "--timeout=60" in options
    assert "--timeout-method=thread" in options
    assert "faulthandler_timeout=45" in options
    suite = tmp_path / "test_hung.py"
    suite.write_text("import threading\ndef test_hung():\n    threading.Event().wait()\n")
    monkeypatch.setenv("PYTEST_ADDOPTS", "")
    monkeypatch.delenv("PYTEST_DISABLE_PLUGIN_AUTOLOAD", raising=False)
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            str(suite),
            "-n",
            "1",
            "--max-worker-restart=0",
            "--timeout=1",
            "--timeout-method=thread",
            "-o",
            "faulthandler_timeout=0.5",
            "--tb=short",
        ],
        capture_output=True,
        text=True,
        timeout=20,
    )
    output = result.stdout + result.stderr
    assert result.returncode == 1, output
    assert "test_hung" in output
    assert "Timeout" in output
    assert "threading.py" in output
    assert "crashed" in output
