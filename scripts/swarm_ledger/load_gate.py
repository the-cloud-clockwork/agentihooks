"""Ten clients write and read a scratch ledger server holding a ledger the size of the live one; red past budget."""

import argparse
import inspect
import itertools
import json
import math
import os
import socket
import subprocess
import sys
import threading
import time
import urllib.request
import uuid
from pathlib import Path
from typing import TextIO

from scripts.swarm_ledger import ledger_server
from scripts.swarm_ledger.api.client import ResourceClient
from scripts.swarm_ledger.repository.sqlite import DATABASE, SQLiteLedgerRepository

authority, core = ledger_server.authority, ledger_server.core
SLUG = "load-gate"
TASKS = 1000
PHASES = 60
FOLLOWUPS = 800
CHAT = 500
ALERTS = 50
COMMENTS = 3
LEDGER_BYTES = 5_000_000
CLIENTS = 10
LIVE_PEAK_WRITES_PER_MINUTE = 29
HEADROOM = 2
TIMEOUT_S = 10.0
SWEEPS = 2
WRITE_P95_S = 2.0
CPU_CORES = 1.0
CPU_WINDOW_S = 10
MIN_WRITES = 0.9
SERVER_WAIT_S = 60.0
ALERT_QUIET_MS = 3_600_000
WORDS = "the swarm keeps every task with its comments proof and the work that landed for the operator".split()


def text(count: int) -> str:
    return " ".join(itertools.islice(itertools.cycle(WORDS), count))


def comment(prefix: str, n: int, at: int) -> dict:
    return {"id": f"c-{prefix}-{n}", "by": f"ci@ab0000-{n % CLIENTS:04d}", "at": at, "text": text(40)}


def task(n: int, at: int, description: str) -> dict:
    return {
        "id": f"t{n}",
        "title": text(14),
        "description": description,
        "phase": f"p{n % PHASES}",
        "lane": "ci" if n % 3 else "eng",
        "state": "done" if n % 4 else "open",
        "claimed_by": "",
        "depends_on": [],
        "territory": ["scripts/swarm_ledger", "tests/swarm_ledger"],
        "kind": "code",
        "done": bool(n % 4),
        "comments": [comment(f"t{n}", k, at) for k in range(COMMENTS)],
    }


def alert(n: int, at: int) -> dict:
    return {
        "id": f"al-{n}",
        "text": text(30),
        "source": "sync",
        "target": "master",
        "state": "done" if n % 2 else "open",
        "at": at,
        "rev": 1,
        "writer": f"ci@ab0000-{n % CLIENTS:04d}",
        "item": f"tasks/t{n}",
        "last_refused_at": at - ALERT_QUIET_MS if n % 4 == 0 else at,
    }


def document(at: int, description: str = "") -> dict:
    doc = {
        "title": "Load gate ledger",
        "overview": text(120),
        "sources": [],
        "phases": [
            {"id": f"p{n}", "description": text(80), "done": False, "comments": [comment(f"p{n}", 0, at)]}
            for n in range(PHASES)
        ],
        "questions": [],
        "notes": [],
        "chat": [{"id": f"m-{n}", "by": "operator", "at": at, "text": text(30)} for n in range(CHAT)],
        "followups": [{"id": f"f-{n}", "text": text(50), "comments": []} for n in range(FOLLOWUPS)],
        "tasks": [task(n, at, description) for n in range(TASKS)],
        "alerts": [alert(n, at) for n in range(ALERTS)],
    }
    events = [
        {"rev": 1, "at": at, "by": "operator", "kind": "comment", "target": f"tasks/t{n % TASKS}", "text": text(20)}
        for n in range(core.EVENTS_KEPT)
    ]
    return {**core.normalize(doc), "_meta": {"rev": 1, "stamps": {}, "events": events, "updated_at": at}}


def size(doc: dict) -> int:
    return len(json.dumps(doc, ensure_ascii=False).encode())


def full_size(at: int) -> dict:
    short = size(document(at))
    words = max(0, math.ceil((LEDGER_BYTES - short) / TASKS / (len(" ".join(WORDS)) / len(WORDS) + 1)))
    while size(doc := document(at, text(words))) < LEDGER_BYTES:
        words += 10
    return doc


def store(folder: Path, doc: dict) -> str:
    repository = SQLiteLedgerRepository(folder / DATABASE)
    repository.import_document(SLUG, doc)
    return repository.token(SLUG)


def p95(samples: list[float]) -> float:
    ordered = sorted(samples)
    return ordered[math.ceil(0.95 * len(ordered)) - 1]


def cpu_seconds(pid: int) -> float:
    fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
    return (int(fields[11]) + int(fields[12])) / os.sysconf("SC_CLK_TCK")


def write_every() -> float:
    """Each client's seconds between writes: all clients together write HEADROOM times the live peak minute."""
    return CLIENTS * 60 / (LIVE_PEAK_WRITES_PER_MINUTE * HEADROOM)


def duration() -> float:
    interval = inspect.signature(ledger_server.watch_ledgers).parameters["interval"].default
    return (SWEEPS + 1) * ledger_server.BIN_SWEEP_EVERY * interval


def busiest_window(samples: list[tuple[float, float]]) -> float | None:
    windows = []
    for start, cpu in samples:
        later = [(at, used) for at, used in samples if at - start >= CPU_WINDOW_S]
        if later:
            at, used = later[0]
            windows.append((used - cpu) / (at - start))
    return max(windows, default=None)


def verdict(writes: list[float], expected: int, cores: float | None, errors: list[str]) -> list[str]:
    problems = [f"{len(errors)} requests failed, first: {errors[0]}"] if errors else []
    if len(writes) < MIN_WRITES * expected:
        problems.append(f"{len(writes)} writes completed of {expected} expected")
    if writes and (worst := p95(writes)) > WRITE_P95_S:
        problems.append(f"write p95 {worst:.3f}s exceeds {WRITE_P95_S}s")
    if cores is None:
        problems.append(f"no {CPU_WINDOW_S}s window of server CPU was sampled")
    elif cores > CPU_CORES:
        problems.append(f"server CPU held {cores:.2f} cores over {CPU_WINDOW_S}s, budget {CPU_CORES}")
    return problems


def unexpired(folder: Path, at: int) -> list[str]:
    """Alerts past their quiet hour that the expiry sweep should have closed during the load."""
    doc = SQLiteLedgerRepository(folder / DATABASE).export_document(SLUG)
    return [alert["id"] for alert in doc.get("alerts", []) if ledger_server.ledger_alerts.expired(alert, at)]


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def start_server(folder: Path, port: int, log: TextIO) -> subprocess.Popen:
    env = {**os.environ, "LEDGER_DIR": str(folder), "LEDGER_PORT": str(port), "SWARM_RELOAD": "0"}
    server = subprocess.Popen(
        [sys.executable, ledger_server.__file__, "--serve"], env=env, stdout=log, stderr=log, stdin=subprocess.DEVNULL
    )
    deadline = time.monotonic() + SERVER_WAIT_S
    while time.monotonic() < deadline:
        if server.poll() is not None:
            raise SystemExit(f"ledger server exited with {server.returncode}")
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=1):
                return server
        except OSError:
            time.sleep(0.2)
    server.kill()
    raise SystemExit(f"ledger server did not listen on port {port} within {SERVER_WAIT_S}s")


def stop_server(server: subprocess.Popen) -> None:
    server.terminate()
    try:
        server.wait(timeout=10)
    except subprocess.TimeoutExpired:
        server.kill()
        server.wait()


def client(api: ResourceClient, name: str, item: str, start: float, deadline: float, results: dict) -> None:
    for n in itertools.count():
        began = start + n * write_every()
        time.sleep(max(0.0, began - time.monotonic()))
        if began >= deadline:
            return
        op = {"op": "add", "thread": f"{item}/comments", "id": f"c-{uuid.uuid4().hex[:10]}", "by": name}
        try:
            sent = time.monotonic()
            reply = api.mutate(SLUG, [{**op, "text": f"Load check write {n} landed."}])
            if rejected := (reply.get("data") or reply).get("rejected"):
                raise ValueError(f"write rejected: {rejected}")
            results["writes"].append(time.monotonic() - sent)
            read = time.monotonic()
            api.request(SLUG, item)
            api.request(SLUG, "tasks?limit=100")
            results["reads"].append(time.monotonic() - read)
        except Exception as exc:  # every failure is a red request, whatever raised it
            results["errors"].append(f"{name}: {type(exc).__name__}: {exc}")


def watcher(base: str, credentials: dict, deadline: float, results: dict) -> None:
    """The operator's page: one event stream held open through the load, as the live page holds it."""
    request = urllib.request.Request(
        f"{base}/api/v1/ledgers/{SLUG}/events", headers={**credentials, "Accept": "text/event-stream"}
    )
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_S) as stream:
            while time.monotonic() < deadline:
                if not stream.read1(65536):
                    raise ConnectionError("the server closed the stream before the load ended")
    except Exception as exc:  # a dropped or silent stream is a red request
        results["errors"].append(f"event stream: {type(exc).__name__}: {exc}")


def sample_cpu(pid: int, deadline: float, results: dict) -> None:
    try:
        while time.monotonic() < deadline:
            results["cpu"].append((time.monotonic(), cpu_seconds(pid)))
            time.sleep(1)
        results["cpu"].append((time.monotonic(), cpu_seconds(pid)))
    except Exception as exc:  # a lost sample leaves the CPU budget unproven
        results["errors"].append(f"cpu sampler: {type(exc).__name__}: {exc}")


def load(port: int, token: str, pid: int, seconds: float) -> dict:
    base = f"http://127.0.0.1:{port}"
    results = {"writes": [], "reads": [], "errors": [], "cpu": []}
    apis = []
    for n in range(CLIENTS):
        name = f"ci@ab0000-{n:04d}"
        credentials = {"X-Ledger-Token": authority.agent_token(token, SLUG, name), "X-Ledger-Agent": name}
        api = ResourceClient(base, credentials, TIMEOUT_S)
        api.mutate(SLUG, [{"op": "join", "id": f"join-{uuid.uuid4().hex[:10]}", "by": name, "role": "member"}])
        apis.append((api, name, f"tasks/t{n * (TASKS // CLIENTS)}"))
    began = time.monotonic()
    deadline = began + seconds
    threads = [
        threading.Thread(target=client, args=(*entry, began + n * write_every() / CLIENTS, deadline, results))
        for n, entry in enumerate(apis)
    ]
    threads.append(threading.Thread(target=watcher, args=(base, {"X-Ledger-Token": token}, deadline, results)))
    threads.append(threading.Thread(target=sample_cpu, args=(pid, deadline, results)))
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    return results


def run(folder: Path) -> list[str]:
    doc = full_size(core.now_ms())
    token = store(folder, doc)
    print(f"ledger {SLUG}: {len(doc['tasks'])} tasks, {size(doc)} bytes", flush=True)
    port = free_port()
    seconds = duration()
    log_path = folder / "server.log"
    with log_path.open("w") as log:
        server = start_server(folder, port, log)
        try:
            results = load(port, token, server.pid, seconds)
        finally:
            stop_server(server)
    writes, cores = results["writes"], busiest_window(results["cpu"])
    expected = CLIENTS * math.ceil(seconds / write_every())
    problems = verdict(writes, expected, cores, results["errors"])
    if "Exception in thread" in (server_log := log_path.read_text()):
        problems.append("a ledger server background thread died")
    if stale := unexpired(folder, core.now_ms()):
        problems.append(f"the expiry sweep left {len(stale)} alerts past their quiet hour open")
    print(
        json.dumps(
            {
                "seconds": seconds,
                "clients": CLIENTS,
                "write_every_s": round(write_every(), 3),
                "writes": len(writes),
                "write_p50_s": round(sorted(writes)[len(writes) // 2], 3) if writes else None,
                "write_p95_s": round(p95(writes), 3) if writes else None,
                "write_max_s": round(max(writes), 3) if writes else None,
                "reads": len(results["reads"]),
                "read_p95_s": round(p95(results["reads"]), 3) if results["reads"] else None,
                "busiest_cores": None if cores is None else round(cores, 3),
                "errors": len(results["errors"]),
                "budget": {"write_p95_s": WRITE_P95_S, "cores": CPU_CORES},
            }
        ),
        flush=True,
    )
    if problems:
        print(server_log[-4000:])
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--folder", type=Path, required=True, help="an empty scratch ledger folder")
    args = parser.parse_args(argv)
    if os.environ.get("GITHUB_ACTIONS") != "true":
        sys.exit("the ledger load gate runs in CI only: it loads the machine for a minute and a half")
    args.folder.mkdir(parents=True, exist_ok=True)
    problems = run(args.folder)
    for problem in problems:
        print(f"::error::{problem}")
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
