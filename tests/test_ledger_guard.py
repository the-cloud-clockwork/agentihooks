import errno
import os
import socket
import subprocess
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

pytestmark = pytest.mark.unit

PLANTED = """
from tests import ledger_guard


def test_writes_the_real_ledger_folder():
    (ledger_guard.REAL / "planted.json").write_text("{}")


def test_hides_its_write_to_the_real_ledger_folder():
    try:
        (ledger_guard.REAL / "hidden.json").write_text("{}")
    except Exception:
        pass


def test_reads_the_real_ledger_folder():
    sorted(ledger_guard.REAL.iterdir())
"""


def pytest_run(home, *args):
    env = {key: value for key, value in os.environ.items() if key not in ("LEDGER_DIR", "LEDGER_PORT")}
    env["HOME"] = str(home)
    command = [sys.executable, "-m", "pytest", "-q", "-p", "no:xdist", "-p", "no:cacheprovider", *args]
    return subprocess.run(command, cwd=ROOT, env=env, capture_output=True, text=True, timeout=300)


def test_files_given_out_of_folder_order_leave_the_real_ledger_folder_alone(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    run = pytest_run(
        home,
        "tests/swarm_ledger/test_hook.py",
        "tests/test_shards.py",
        "tests/swarm_ledger/test_reopen.py",
        "-k",
        "test_reopen_clears_closed_time",
    )
    assert run.returncode == 0, run.stdout + run.stderr
    assert not (home / "development-ledger").exists()


def test_a_test_that_reaches_the_real_ledger_folder_fails(tmp_path):
    home = tmp_path / "home"
    (home / "development-ledger").mkdir(parents=True)
    planted = tmp_path / "test_planted.py"
    planted.write_text(PLANTED)
    run = pytest_run(home, "-p", "tests.conftest", "--rootdir", str(ROOT), str(planted), "-rA")
    report = run.stdout + run.stderr
    for name in ("writes_the_real", "hides_its_write", "reads_the_real"):
        assert any(name in line and ("FAILED" in line or "ERROR" in line) for line in report.splitlines()), report
    assert list((home / "development-ledger").iterdir()) == []


def test_every_test_runs_on_the_suite_ledger_folder_and_a_spare_port():
    from tests import ledger_guard

    assert Path(os.environ["LEDGER_DIR"]) == ledger_guard.SUITE
    assert ledger_guard.REAL not in (ledger_guard.SUITE, *ledger_guard.SUITE.parents)
    assert os.environ["LEDGER_PORT"] != "8765"


def bind_refusal(port):
    with socket.socket() as other, pytest.raises(OSError) as taken:
        other.bind(("127.0.0.1", port))
    return taken.value.errno


def test_the_suite_port_stays_held_so_no_other_worker_is_handed_it():
    assert bind_refusal(int(os.environ["LEDGER_PORT"])) == errno.EADDRINUSE


def test_a_reserved_port_refuses_other_binds_and_still_takes_its_own_server(ledger_port):
    assert bind_refusal(ledger_port) == errno.EADDRINUSE
    ThreadingHTTPServer(("127.0.0.1", ledger_port), BaseHTTPRequestHandler).server_close()
