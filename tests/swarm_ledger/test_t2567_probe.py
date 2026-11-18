"""Probe for t2567: which condition makes the ledger schema setup raise database is locked, and how fast."""

import json
import os
import signal
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SERVER = ROOT / "scripts" / "swarm_ledger" / "ledger_server.py"
CPUS = sorted(os.sched_getaffinity(0))
REPORTS = Path(os.environ.get("PROOF_REPORT_DIR") or "/tmp/proof-reports")
TRIALS = int(os.environ.get("PROBE_TRIALS", "150"))
RELOADS = int(os.environ.get("PROBE_RELOADS", "10"))

CHILD = r"""
import json, os, sys, time, traceback
from pathlib import Path
root = sys.argv[1]
sys.path[:0] = [root, os.path.join(root, "scripts", "swarm_ledger")]
from scripts.swarm_ledger.repository.sqlite import SQLiteLedgerRepository
cpu = int(sys.argv[2])
if cpu >= 0:
    os.sched_setaffinity(0, {cpu})
mode = sys.argv[3]
print("ready", flush=True)
for line in sys.stdin:
    path, _, extra = line.strip().partition(" ")
    started = time.monotonic()
    result = {"ok": True}
    try:
        if mode == "setup":
            SQLiteLedgerRepository(Path(path)).summaries()
        elif mode == "write":
            repo = SQLiteLedgerRepository(Path(path))
            stop = Path(extra)
            count = 0
            while not stop.exists():
                with repo.connect() as connection, connection:
                    connection.execute("BEGIN IMMEDIATE")
                    connection.execute("INSERT OR REPLACE INTO registry VALUES ('probe', ?, '1')", (str(count % 50),))
                    time.sleep(0.003)
                count += 1
            result["writes"] = count
        elif mode == "churn":
            repo = SQLiteLedgerRepository(Path(path))
            stop = Path(extra)
            count = 0
            while not stop.exists():
                with repo.connect() as connection:
                    connection.execute("SELECT count(*) FROM ledgers").fetchone()
                count += 1
            result["reads"] = count
    except Exception as exc:
        frames = traceback.extract_tb(exc.__traceback__)
        result = {
            "ok": False,
            "error": f"{type(exc).__name__}: {exc}",
            "frames": [f"{frame.name}: {frame.line}" for frame in frames[-4:]],
        }
    result["elapsed"] = round(time.monotonic() - started, 4)
    print(json.dumps(result), flush=True)
"""


class Child:
    def __init__(self, cpu: int, mode: str, folder: Path) -> None:
        env = {**os.environ, "LEDGER_DIR": str(folder), "PYTHONPATH": str(ROOT)}
        self.process = subprocess.Popen(
            [sys.executable, "-c", CHILD, str(ROOT), str(cpu), mode],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            text=True,
            env=env,
        )
        assert self.process.stdin is not None and self.process.stdout is not None
        self.stdin, self.stdout = self.process.stdin, self.process.stdout
        assert self.stdout.readline().strip() == "ready"

    def send(self, line: str) -> None:
        self.stdin.write(line + "\n")
        self.stdin.flush()

    def result(self) -> dict:
        return json.loads(self.stdout.readline())

    def close(self) -> None:
        self.stdin.close()
        self.process.wait(30)


def report(name: str, data: dict) -> dict:
    data = {
        "scenario": name,
        "sqlite": sqlite3.sqlite_version,
        "python": sys.version.split()[0],
        "cpus": len(CPUS),
        **data,
    }
    REPORTS.mkdir(parents=True, exist_ok=True)
    (REPORTS / f"t2567-{name}.json").write_text(json.dumps(data, indent=1))
    return data


def summary(results: list[dict]) -> dict:
    failed = [result for result in results if not result["ok"]]
    return {
        "runs": len(results),
        "failed": len(failed),
        "errors": sorted({result["error"] for result in failed}),
        "failed_elapsed_max": max((result["elapsed"] for result in failed), default=None),
        "ok_elapsed_max": max((result["elapsed"] for result in results if result["ok"]), default=None),
        "first_failures": failed[:3],
    }


def pair(tmp_path: Path, cpu_a: int, cpu_b: int) -> dict:
    a, b = Child(cpu_a, "setup", tmp_path), Child(cpu_b, "setup", tmp_path)
    results = []
    try:
        for trial in range(TRIALS):
            folder = tmp_path / f"fresh-{trial}"
            folder.mkdir()
            first, second = (a, b) if trial % 2 == 0 else (b, a)
            first.send(str(folder / "ledgers.sqlite3"))
            second.send(str(folder / "ledgers.sqlite3"))
            results.extend([first.result(), second.result()])
    finally:
        a.close()
        b.close()
    return summary(results)


@pytest.mark.wall_clock
@pytest.mark.xdist_group("t2567-fresh-pinned")
def test_fresh_pair_pinned_to_one_core(tmp_path: Path) -> None:
    data = report("fresh-pair-one-core", pair(tmp_path, CPUS[0], CPUS[0]))
    assert data["failed"] == 0, json.dumps(data, indent=1)


@pytest.mark.wall_clock
@pytest.mark.xdist_group("t2567-fresh-free")
def test_fresh_pair_on_two_cores(tmp_path: Path) -> None:
    data = report("fresh-pair-two-cores", pair(tmp_path, CPUS[1 % len(CPUS)], CPUS[2 % len(CPUS)]))
    assert data["failed"] == 0, json.dumps(data, indent=1)


def against(tmp_path: Path, mode: str, cpu: int) -> dict:
    path = tmp_path / "ledgers.sqlite3"
    setup = Child(cpu, "setup", tmp_path)
    other = Child(cpu, mode, tmp_path)
    stop = tmp_path / "stop"
    results = []
    try:
        setup.send(str(path))
        assert setup.result()["ok"]
        other.send(f"{path} {stop}")
        time.sleep(0.2)
        for _ in range(TRIALS):
            setup.send(str(path))
            results.append(setup.result())
        stop.touch()
        background = other.result()
    finally:
        stop.touch()
        setup.close()
        other.close()
    return {**summary(results), "background": background}


@pytest.mark.wall_clock
@pytest.mark.xdist_group("t2567-writer")
def test_setup_against_a_writer_on_one_core(tmp_path: Path) -> None:
    data = report("wal-writer-one-core", against(tmp_path, "write", CPUS[3 % len(CPUS)]))
    assert data["failed"] == 0 and data["background"]["ok"], json.dumps(data, indent=1)


@pytest.mark.wall_clock
@pytest.mark.xdist_group("t2567-churn")
def test_setup_against_connection_churn_on_one_core(tmp_path: Path) -> None:
    data = report("wal-churn-one-core", against(tmp_path, "churn", CPUS[4 % len(CPUS)]))
    assert data["failed"] == 0 and data["background"]["ok"], json.dumps(data, indent=1)


def get(port: int, path: str) -> int | str:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=30) as reply:
            return reply.status
    except Exception as exc:
        return repr(exc)


def pid_of(folder: Path) -> int | None:
    try:
        return int((folder / ".server.pid").read_text())
    except (OSError, ValueError):
        return None


def until(check, limit: float, step: float = 0.02) -> bool:
    deadline = time.monotonic() + limit
    while time.monotonic() < deadline:
        if check():
            return True
        time.sleep(step)
    return False


def alive(pid: int) -> bool:
    try:
        state = Path(f"/proc/{pid}/stat").read_text().rpartition(")")[2].split()[0]
    except (OSError, IndexError):
        return False
    return state != "Z"


def reload_trial(folder: Path, port: int, cpu: int, hammer: str, start: str) -> dict:
    folder.mkdir()
    env = {
        **os.environ,
        "PYTHONPATH": str(ROOT),
        "LEDGER_DIR": str(folder),
        "LEDGER_PORT": str(port),
        "SWARM_RELOAD": "0",
    }
    env.pop("LEDGER_RUN_PID", None)
    env.pop("LEDGER_RUN_START", None)
    log = folder / "server.out"
    with log.open("w") as out:
        first = subprocess.Popen(
            [sys.executable, str(SERVER), "--serve"],
            env=env,
            stdout=out,
            stderr=out,
            preexec_fn=lambda: os.sched_setaffinity(0, {cpu}),
        )
    stop = threading.Event()
    answers: list[int | str] = []

    def loop() -> None:
        while not stop.is_set():
            answers.append(get(port, hammer))
            time.sleep(0.005)

    worker = threading.Thread(target=loop, daemon=True)
    outcome = "started"
    successor = None
    started = time.monotonic()
    try:
        if not until(lambda: pid_of(folder) == first.pid, 60) or get(port, "/healthz") != 200:
            return {"outcome": "never served", "locked": False, "log": log.read_text()[-3000:]}
        if start == "before":
            worker.start()
            time.sleep(0.3)
        else:
            time.sleep(0.2)
        os.kill(first.pid, signal.SIGHUP)
        started = time.monotonic()
        if start == "file":
            until(lambda: (folder / "ledgers.sqlite3").exists(), 60, 0.0005)
            worker.start()
        finished = until(lambda: first.poll() is not None or "never reported ready" in log.read_text(), 90)
        successor = pid_of(folder) if first.poll() is not None else None
        outcome = "handed over" if successor not in (None, first.pid) else ("never ready" if finished else "timeout")
    finally:
        stop.set()
        if worker.is_alive():
            worker.join(30)
        for pid in {first.pid, successor} - {None}:
            try:
                os.kill(pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
        try:
            first.wait(timeout=15)
        except subprocess.TimeoutExpired:
            first.kill()
        if successor:
            until(lambda: not alive(successor), 15)
    text = log.read_text()
    locked = "database is locked" in text
    return {
        "outcome": outcome,
        "seconds": round(time.monotonic() - started, 2),
        "locked": locked,
        "answers": sorted({str(answer) for answer in answers}),
        "log": text[-4000:] if locked or outcome != "handed over" else "",
    }


def reloads(tmp_path: Path, port: int, cpu: int, hammer: str, start: str) -> dict:
    trials = [reload_trial(tmp_path / f"reload-{n}", port, cpu, hammer, start) for n in range(RELOADS)]
    return {
        "trials": len(trials),
        "locked": sum(trial["locked"] for trial in trials),
        "outcomes": [trial["outcome"] for trial in trials],
        "seconds": [trial.get("seconds") for trial in trials],
        "failures": [trial for trial in trials if trial["locked"] or trial["outcome"] != "handed over"][:3],
    }


@pytest.mark.wall_clock
@pytest.mark.xdist_group("t2567-reload-health")
def test_reload_with_health_requests_on_one_core(tmp_path: Path, ledger_port: int) -> None:
    data = report("reload-health-one-core", reloads(tmp_path, ledger_port, CPUS[5 % len(CPUS)], "/healthz", "before"))
    assert data["locked"] == 0, json.dumps(data, indent=1)


@pytest.mark.wall_clock
@pytest.mark.xdist_group("t2567-reload-read-file")
def test_reload_with_first_ledger_read_as_the_successor_creates_the_file_on_one_core(
    tmp_path: Path, ledger_port: int
) -> None:
    data = report(
        "reload-read-at-file-one-core",
        reloads(tmp_path, ledger_port, CPUS[6 % len(CPUS)], "/api/v1/ledgers", "file"),
    )
    assert data["locked"] == 0, json.dumps(data, indent=1)


@pytest.mark.wall_clock
@pytest.mark.xdist_group("t2567-reload-read-before")
def test_reload_with_ledger_reads_before_and_during_on_one_core(tmp_path: Path, ledger_port: int) -> None:
    data = report(
        "reload-read-before-one-core",
        reloads(tmp_path, ledger_port, CPUS[7 % len(CPUS)], "/api/v1/ledgers", "before"),
    )
    assert data["locked"] == 0, json.dumps(data, indent=1)
