import ctypes
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts.swarm_ledger import server_supervisor

ROOT = Path(__file__).resolve().parents[2]
PR_GET_CHILD_SUBREAPER = 37


class Libc:
    def __init__(self, answer=0):
        self.calls, self.answer = [], answer

    def prctl(self, *args):
        self.calls.append(args)
        return self.answer


def test_subreaping_asks_the_kernel_to_adopt_orphaned_descendants():
    libc = Libc()
    server_supervisor.subreap(libc)
    assert libc.calls == [(36, 1, 0, 0, 0)]
    assert server_supervisor.PR_SET_CHILD_SUBREAPER == 36


def test_a_refused_subreaper_stops_the_supervisor_before_it_starts_a_server():
    with pytest.raises(OSError) as refused:
        server_supervisor.subreap(Libc(answer=-1))
    assert refused.value.args == ("prctl refused PR_SET_CHILD_SUBREAPER",)
    assert server_supervisor.REFUSED == "prctl refused PR_SET_CHILD_SUBREAPER"


@pytest.mark.skipif(sys.platform != "linux", reason="child subreapers are a Linux feature")
def test_subreaping_without_a_library_marks_this_process_a_subreaper():
    libc = ctypes.CDLL(None)
    held = ctypes.c_int()
    try:
        server_supervisor.subreap()
        libc.prctl(PR_GET_CHILD_SUBREAPER, ctypes.byref(held), 0, 0, 0)
    finally:
        libc.prctl(36, 0, 0, 0, 0)
    assert held.value == 1


def test_a_forwarded_signal_reaches_the_pidfile_server_and_every_held_child(tmp_path):
    pidfile = tmp_path / ".server.pid"
    pidfile.write_text("41")
    sent = []
    server_supervisor.forward(pidfile, signal.SIGTERM, (52, 41), lambda pid, sig: sent.append((pid, sig)))
    assert sent == [(41, signal.SIGTERM), (52, signal.SIGTERM)]


@pytest.mark.parametrize("text", [None, "", "x"])
def test_a_forward_without_a_readable_pidfile_still_reaches_the_held_child(tmp_path, text):
    pidfile = tmp_path / ".server.pid"
    if text is not None:
        pidfile.write_text(text)
    sent = []
    server_supervisor.forward(pidfile, signal.SIGTERM, (52,), lambda pid, sig: sent.append((pid, sig)))
    assert sent == [(52, signal.SIGTERM)]
    server_supervisor.forward(pidfile, signal.SIGTERM, kill=lambda pid, sig: sent.append((pid, sig)))
    assert sent == [(52, signal.SIGTERM)]


def test_a_forward_goes_on_past_a_server_already_gone(tmp_path):
    pidfile = tmp_path / ".server.pid"
    pidfile.write_text("41")
    sent = []

    def kill(pid, sig):
        sent.append(pid)
        if pid == 41:
            raise ProcessLookupError(pid)

    server_supervisor.forward(pidfile, signal.SIGTERM, (52,), kill)
    assert sent == [41, 52]


def test_reaping_waits_until_no_child_remains_and_returns_the_last_exit_code():
    statuses = iter([(10, 0), (11, 3 << 8)])

    def wait():
        for status in statuses:
            return status
        raise ChildProcessError

    assert server_supervisor.reap(wait) == 3


def test_reaping_a_server_ended_by_a_signal_returns_the_shell_code():
    statuses = iter([(10, signal.SIGTERM)])

    def wait():
        for status in statuses:
            return status
        raise ChildProcessError

    assert server_supervisor.reap(wait) == 128 + signal.SIGTERM


def test_reaping_with_no_child_returns_zero():
    def wait():
        raise ChildProcessError

    assert server_supervisor.reap(wait) == 0


def test_supervising_adopts_orphans_forwards_stop_signals_and_reaps(tmp_path, monkeypatch):
    libc, installed, spawned, forwarded = Libc(), {}, [], []
    monkeypatch.setattr(server_supervisor, "forward", lambda *args: forwarded.append(args))
    status = server_supervisor.supervise(
        ["serve"],
        tmp_path / ".server.pid",
        spawn=lambda argv: spawned.append(argv) or SimpleNamespace(pid=52),
        libc=libc,
        wait=lambda: (_ for _ in ()).throw(ChildProcessError),
        install=lambda signum, handler: installed.update({signum: handler}),
    )
    assert (status, spawned, libc.calls) == (0, [["serve"]], [(36, 1, 0, 0, 0)])
    assert set(installed) == {signal.SIGTERM, signal.SIGINT}
    installed[signal.SIGTERM](signal.SIGTERM, None)
    assert forwarded == [(tmp_path / ".server.pid", signal.SIGTERM, (52,))]


def test_the_supervisor_runs_this_folder_s_server_on_the_ledger_folder_pidfile(monkeypatch):
    seen = []
    monkeypatch.setattr(server_supervisor, "supervise", lambda argv, pidfile: seen.append((argv, pidfile)) or 7)
    assert server_supervisor.main({"LEDGER_DIR": "/ledgers"}) == 7
    server = str(ROOT / "scripts" / "swarm_ledger" / "ledger_server.py")
    assert seen == [([sys.executable, server, "--serve"], Path("/ledgers/.server.pid"))]


ORPHANING = (
    "import subprocess, sys\n"
    "subprocess.Popen([sys.executable, '-c', 'import time, pathlib, sys\\n"
    'time.sleep(0.5)\\npathlib.Path(sys.argv[1]).write_text("done")\', sys.argv[1]], start_new_session=True)\n'
)


@pytest.mark.wall_clock
@pytest.mark.skipif(sys.platform != "linux", reason="child subreapers are a Linux feature")
def test_the_supervisor_outlives_a_server_that_leaves_its_successor_behind(tmp_path):
    marker = tmp_path / "successor"
    script = (
        "import sys\n"
        "from pathlib import Path\n"
        "from scripts.swarm_ledger import server_supervisor\n"
        "sys.exit(server_supervisor.supervise([sys.executable, '-c', sys.argv[1], sys.argv[2]], Path(sys.argv[3])))\n"
    )
    started = time.monotonic()
    done = subprocess.run(
        [sys.executable, "-c", script, ORPHANING, str(marker), str(tmp_path / ".server.pid")],
        env={**os.environ, "PYTHONPATH": str(ROOT)},
        timeout=30,
        check=False,
    )
    assert done.returncode == 0
    assert marker.read_text() == "done"
    assert time.monotonic() - started >= 0.5
