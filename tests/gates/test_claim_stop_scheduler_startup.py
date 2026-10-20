import fcntl
import subprocess
import sys

import pytest

from tests.gates.test_claim_stop_executor import (
    test_a_lifted_stop_never_blocks_the_claim_and_clear_restarts_the_streak as lifted_stop,
)

pytest_plugins = ["tests.gates.test_claim_stop_executor"]


@pytest.fixture(autouse=True)
def supervisor_starts_during_readiness_probe(tmp_path, monkeypatch):
    from tests.scheduler import process_service

    gate = tmp_path / "start-supervisor"
    processes = []
    popen = subprocess.Popen
    flock = fcntl.flock

    def delayed_supervisor(argv, **kwargs):
        if argv[-2:] == ["scripts.scheduler.service", "supervise"]:
            argv = [
                sys.executable,
                "-c",
                "import pathlib, runpy, sys, time; "
                f"gate = pathlib.Path({str(gate)!r}); "
                "deadline = time.monotonic() + 1; "
                "exec('while not gate.exists() and time.monotonic() < deadline: time.sleep(0.01)'); "
                "sys.argv = ['scripts.scheduler.service', 'supervise']; "
                "runpy.run_module('scripts.scheduler.service', run_name='__main__')",
            ]
            process = popen(argv, **kwargs)
            processes.append(process)
            return process
        return popen(argv, **kwargs)

    def held_probe(handle, operation):
        result = flock(handle, operation)
        if operation == fcntl.LOCK_EX | fcntl.LOCK_NB and not gate.exists():
            gate.touch()
            processes[0].wait(timeout=10)
        return result

    monkeypatch.setattr(process_service.subprocess, "Popen", delayed_supervisor)
    monkeypatch.setattr(process_service.fcntl, "flock", held_probe)


@pytest.mark.parametrize("scope", ["session", "agent", "task"])
def test_lifted_stop_passes_when_supervisor_starts_during_the_readiness_probe(rig, scope):
    lifted_stop(rig, scope)
