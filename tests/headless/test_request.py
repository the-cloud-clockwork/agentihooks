import subprocess
import sys

import pytest

from scripts.headless import request

pytestmark = pytest.mark.unit


def test_execute_closes_stdin_and_bounds_the_run(monkeypatch):
    seen = {}

    def fake_run(command, **kwargs):
        seen.update(kwargs, command=command)
        return subprocess.CompletedProcess(command, 0, "out", "")

    monkeypatch.setattr(request.subprocess, "run", fake_run)
    assert request.execute(["tool", "arg"], {"PATH": "/bin"}).stdout == "out"
    assert seen == {
        "command": ["tool", "arg"],
        "env": {"PATH": "/bin"},
        "stdin": subprocess.DEVNULL,
        "capture_output": True,
        "text": True,
        "timeout": 900,
        "check": False,
    }


def test_execute_raises_with_the_exit_code_and_never_the_stderr():
    script = "import sys; sys.stderr.write('x' * 600 + 'TAIL\\n'); sys.exit(3)"
    with pytest.raises(request.HeadlessError) as caught:
        request.execute([sys.executable, "-c", script], {})
    assert str(caught.value) == f"{sys.executable} exited 3"


def test_execute_turns_a_timeout_into_a_headless_error(monkeypatch):
    def fake_run(command, **kwargs):
        raise subprocess.TimeoutExpired(command, kwargs["timeout"])

    monkeypatch.setattr(request.subprocess, "run", fake_run)
    with pytest.raises(request.HeadlessError) as caught:
        request.execute(["tool"], {})
    assert str(caught.value) == "tool ran past 900 seconds"
    assert isinstance(caught.value.__cause__, subprocess.TimeoutExpired)
