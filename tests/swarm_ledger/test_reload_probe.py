"""Repeat the ledger server reload handoff and dump every stuck successor's threads when its pid never appears."""

import os
import signal
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SERVER = ROOT / "scripts" / "swarm_ledger" / "ledger_server.py"


def pid_of(folder: Path) -> int | None:
    """The pid the server folder names."""
    try:
        return int((folder / ".server.pid").read_text())
    except (OSError, ValueError):
        return None


def servers_of(folder: Path) -> list[int]:
    """Every live ledger server process serving folder."""
    found = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            argv = (entry / "cmdline").read_bytes().split(b"\0")
            environ = (entry / "environ").read_bytes().split(b"\0")
        except OSError:
            continue
        if str(SERVER).encode() in argv and f"LEDGER_DIR={folder}".encode() in environ:
            found.append(int(entry.name))
    return found


def state_of(pid: int) -> str:
    """The proc status lines and kernel wait channel of pid."""
    try:
        status = Path(f"/proc/{pid}/status").read_text().splitlines()
        wchan = Path(f"/proc/{pid}/wchan").read_text()
    except OSError as exc:
        return repr(exc)
    return " ".join(line for line in status if line.split(":")[0] in {"State", "PPid", "Threads"}) + f" wchan={wchan}"


@pytest.mark.wall_clock
@pytest.mark.parametrize("round", range(40))
def test_reload_probe(tmp_path: Path, ledger_port: int, round: int) -> None:
    """One reload handoff under load that reports the successor's threads when it never publishes its pid."""
    env = {
        **os.environ,
        "PYTHONPATH": str(ROOT),
        "PYTHONFAULTHANDLER": "1",
        "LEDGER_DIR": str(tmp_path),
        "LEDGER_PORT": str(ledger_port),
        "SWARM_RELOAD": "0",
    }
    env.pop("LEDGER_RUN_PID", None)
    env.pop("LEDGER_RUN_START", None)
    out = tmp_path / "server.out"
    with out.open("w") as log:
        first = subprocess.Popen([sys.executable, str(SERVER), "--serve"], env=env, stdout=log, stderr=log)
    stop = threading.Event()

    def hammer() -> None:
        while not stop.is_set():
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{ledger_port}/healthz", timeout=5).read()
            except OSError:
                pass
            time.sleep(0.005)

    try:
        deadline = time.monotonic() + 30
        while pid_of(tmp_path) != first.pid:
            assert time.monotonic() < deadline, out.read_text()
            time.sleep(0.02)
        threading.Thread(target=hammer, daemon=True).start()
        time.sleep(0.2)
        signalled = time.monotonic()
        os.kill(first.pid, signal.SIGHUP)
        first.wait(timeout=60)
        exited = time.monotonic() - signalled
        deadline = time.monotonic() + 30
        while pid_of(tmp_path) in (None, first.pid):
            if time.monotonic() > deadline:
                stuck = servers_of(tmp_path)
                states = {pid: state_of(pid) for pid in stuck}
                for pid in stuck:
                    os.kill(pid, signal.SIGABRT)
                time.sleep(2)
                pytest.fail(f"exited after {exited:.2f}s; stuck {states}\n{out.read_text()}")
            time.sleep(0.02)
    finally:
        stop.set()
        for pid in servers_of(tmp_path):
            os.kill(pid, signal.SIGTERM)
