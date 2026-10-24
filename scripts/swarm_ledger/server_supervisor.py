import ctypes
import os
import signal
import subprocess
import sys
from pathlib import Path

PR_SET_CHILD_SUBREAPER = 36
FORWARDED = (signal.SIGTERM, signal.SIGINT)
REFUSED = "prctl refused PR_SET_CHILD_SUBREAPER"


def subreap(libc=None) -> None:
    lib = libc or ctypes.CDLL(None)
    if lib.prctl(PR_SET_CHILD_SUBREAPER, 1, 0, 0, 0) != 0:
        raise OSError(REFUSED)


def forward(pidfile: Path, signum: int, held: tuple[int, ...] = (), kill=os.kill) -> None:
    pids = set(held)
    try:
        pids.add(int(pidfile.read_text()))
    except (OSError, ValueError):
        pass
    for pid in sorted(pids):
        try:
            kill(pid, signum)
        except OSError:
            continue


def reap(wait=os.wait) -> int:
    status = 0
    while True:
        try:
            _, status = wait()
        except ChildProcessError:
            code = os.waitstatus_to_exitcode(status)
            return code if code >= 0 else 128 - code


def supervise(
    argv: list[str], pidfile: Path, spawn=subprocess.Popen, libc=None, wait=os.wait, install=signal.signal
) -> int:
    subreap(libc)
    child = spawn(argv)
    for signum in FORWARDED:
        install(signum, lambda received, frame: forward(pidfile, received, (child.pid,)))
    return reap(wait)


def main(environ=os.environ) -> int:
    server = Path(__file__).resolve().parent / "ledger_server.py"
    return supervise([sys.executable, str(server), "--serve"], Path(environ["LEDGER_DIR"]) / ".server.pid")


if __name__ == "__main__":
    sys.exit(main())
