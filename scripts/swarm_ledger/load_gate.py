"""A scratch ledger server on a ledger the size of the live one, written and read by ten clients at once.

CI fails when write p95 or the server's CPU breaks its budget, so a slow ledger change never merges. The ledger
size, the load and the budgets are the constants below; the run lasts long enough to cover every background sweep
of the watch loop at least twice.
"""

import argparse
import inspect
import itertools
import json
import math
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import uuid
from pathlib import Path

from scripts.swarm_ledger import ledger_server
from scripts.swarm_ledger.api.client import ResourceClient

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
HEADROOM = 4
TIMEOUT_S = 10.0
SWEEPS = 2
WRITE_P95_S = 2.0
CPU_CORES = 1.0
SERVER_WAIT_S = 60.0
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
        "last_refused_at": at,
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
    """The generated ledger, its task descriptions padded until the whole document reaches LEDGER_BYTES."""
    short = size(document(at))
    words = max(0, math.ceil((LEDGER_BYTES - short) / TASKS / (len(" ".join(WORDS)) / len(WORDS) + 1)))
    while size(doc := document(at, text(words))) < LEDGER_BYTES:
        words += 10
    return doc


def store(folder: Path, doc: dict) -> str:
    from scripts.swarm_ledger.repository.sqlite import DATABASE, SQLiteLedgerRepository

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
    """Each client's seconds between writes, so all clients together write HEADROOM times the live peak minute."""
    return CLIENTS * 60 / (LIVE_PEAK_WRITES_PER_MINUTE * HEADROOM)


def duration() -> float:
    """Long enough for the watch loop's slowest sweep to run SWEEPS times during the load."""
    interval = inspect.signature(ledger_server.watch_ledgers).parameters["interval"].default
    return (SWEEPS + 1) * ledger_server.BIN_SWEEP_EVERY * interval


def verdict(writes: list[float], cores: float, errors: list[str]) -> list[str]:
    problems = [f"{len(errors)} requests failed, first: {errors[0]}"] if errors else []
    if not writes:
        return [*problems, "no write completed"]
    if p95(writes) > WRITE_P95_S:
        problems.append(f"write p95 {p95(writes):.3f}s exceeds {WRITE_P95_S}s")
    if cores > CPU_CORES:
        problems.append(f"server CPU averaged {cores:.2f} cores, budget {CPU_CORES}")
    return problems


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def start_server(folder: Path, port: int, log) -> subprocess.Popen:
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


def client(api: ResourceClient, name: str, item: str, start: float, deadline: float, results: dict) -> None:
    for n in itertools.count():
        began = start + n * write_every()
        time.sleep(max(0.0, began - time.monotonic()))
        if began >= deadline:
            return
        op = {"op": "add", "thread": f"{item}/comments", "id": f"c-{uuid.uuid4().hex[:10]}", "by": name}
        try:
            sent = time.monotonic()
            api.mutate(SLUG, [{**op, "text": f"Load check write {n} landed."}])
            results["writes"].append(time.monotonic() - sent)
            read = time.monotonic()
            api.request(SLUG, item)
            api.request(SLUG, "tasks?limit=100")
            results["reads"].append(time.monotonic() - read)
        except (OSError, urllib.error.URLError, ValueError) as exc:
            results["errors"].append(f"{name}: {exc}")


def load(port: int, token: str, seconds: float) -> dict:
    base = f"http://127.0.0.1:{port}"
    results = {"writes": [], "reads": [], "errors": []}
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
    with (folder / "server.log").open("w") as log:
        server = start_server(folder, port, log)
        try:
            seconds = duration()
            cpu, wall = cpu_seconds(server.pid), time.monotonic()
            results = load(port, token, seconds)
            cores = (cpu_seconds(server.pid) - cpu) / (time.monotonic() - wall)
        finally:
            server.terminate()
            server.wait(timeout=10)
    writes, reads = results["writes"], results["reads"]
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
                "reads": len(reads),
                "read_p95_s": round(p95(reads), 3) if reads else None,
                "server_cores": round(cores, 3),
                "errors": len(results["errors"]),
                "budget": {"write_p95_s": WRITE_P95_S, "cores": CPU_CORES},
            }
        ),
        flush=True,
    )
    return verdict(writes, cores, results["errors"])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--folder", type=Path, help="scratch ledger folder, default a new temporary one")
    args = parser.parse_args(argv)
    folder = args.folder or Path(tempfile.mkdtemp(prefix="ledger-load-"))
    folder.mkdir(parents=True, exist_ok=True)
    problems = run(folder)
    for problem in problems:
        print(f"::error::{problem}")
    print((folder / "server.log").read_text()[-4000:] if problems else "ledger writes stayed within budget")
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
