import subprocess

import pytest

from tests.gates import test_claim_stop_executor, test_push_stop_executor


@pytest.mark.parametrize("executor", [test_claim_stop_executor, test_push_stop_executor])
def test_join_failure_reports_the_server_traceback(executor, tmp_path, monkeypatch, ledger_port):
    run = subprocess.run
    traceback = "Traceback (most recent call last):\nRuntimeError: planted server failure\n"

    def fail_join(argv, **kwargs):
        if str(executor.SCRIPTS / "ledger.py") in argv:
            (tmp_path / "ledgers" / ".server.log").write_text(traceback)
            return subprocess.CompletedProcess(argv, 1, "", "Remote end closed connection without response\n")
        return run(argv, **kwargs)

    monkeypatch.setattr(subprocess, "run", fail_join)
    fixture = executor.rig.__wrapped__(tmp_path, monkeypatch, ledger_port)
    with pytest.raises((AssertionError, pytest.fail.Exception)) as failure:
        next(fixture)
    message = str(failure.value)
    assert "Remote end closed connection without response" in message
    assert traceback in message
