"""Ten clients write and read a scratch ledger server holding a ledger the size of the live one; red past budget."""

import argparse
import ast
import collections
import inspect
import itertools
import json
import math
import os
import socket
import statistics
import subprocess
import sys
import threading
import time
import urllib.request
import uuid
from pathlib import Path
from typing import BinaryIO, TextIO

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
READS_PER_SECOND = 1.0
WRITE_P95_S = 2.0
CPU_CORES = 1.0
CPU_WINDOW_S = 10
RESOURCE_WARMUP_S = 20
RESOURCE_WINDOW_S = 60
THREAD_GROWTH = 8
RSS_GROWTH_KIB = 65_536
MIN_WRITES = 0.9
SERVER_WAIT_S = 60.0
STDERR_TAIL_LINES = 200
RELAY_JOIN_S = 5.0
ALERT_QUIET_MS = 3_600_000
WRITE_BREACH = "write p95"
BUDGETS = {
    "WRITE_P95_S": min,
    "CPU_CORES": min,
    "CPU_WINDOW_S": min,
    "RESOURCE_WARMUP_S": min,
    "RESOURCE_WINDOW_S": max,
    "THREAD_GROWTH": min,
    "RSS_GROWTH_KIB": min,
    "LEDGER_BYTES": max,
    "CLIENTS": max,
    "LIVE_PEAK_WRITES_PER_MINUTE": max,
    "HEADROOM": max,
    "MIN_WRITES": max,
    "READS_PER_SECOND": max,
    "SWEEPS": max,
}
RELAYS: dict[int, threading.Thread] = {}
WORDS = "the swarm keeps every task with its comments proof and the work that landed for the operator".split()


def text(count: int) -> str:
    return " ".join(WORDS[n % len(WORDS)] for n in range(count))


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
    estimate = max(0, math.ceil((LEDGER_BYTES - short) / TASKS / (len(" ".join(WORDS)) / len(WORDS) + 1)))
    for words in range(estimate, estimate + 1000, 10):
        if size(doc := document(at, text(words))) >= LEDGER_BYTES:
            return doc
    raise ValueError(f"no padding within a thousand words reaches {LEDGER_BYTES} bytes")


def store(folder: Path, doc: dict) -> str:
    repository = SQLiteLedgerRepository(folder / DATABASE)
    repository.import_document(SLUG, doc)
    return repository.token(SLUG)


def p95(samples: list[float]) -> float:
    ordered = sorted(samples)
    return ordered[math.ceil(0.95 * len(ordered)) - 1]


def p95_or_none(samples: list[float]) -> float | None:
    return p95(samples) if samples else None


def cpu_seconds(pid: int) -> float:
    fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
    return (int(fields[11]) + int(fields[12])) / os.sysconf("SC_CLK_TCK")


def server_resources(pid: int) -> tuple[int, int]:
    fields = dict(line.split(":", 1) for line in Path(f"/proc/{pid}/status").read_text().splitlines())
    return int(fields["Threads"]), int(fields["VmRSS"].split()[0])


def resource_verdict(samples: list[tuple[float, int, int]]) -> list[str]:
    if not samples:
        return [f"server resource sampling did not cover {RESOURCE_WINDOW_S}s after warmup"]
    warmed = [sample for sample in samples if sample[0] >= samples[0][0] + RESOURCE_WARMUP_S]
    if len(warmed) < 3 or warmed[-1][0] - warmed[0][0] < RESOURCE_WINDOW_S:
        return [f"server resource sampling did not cover {RESOURCE_WINDOW_S}s after warmup"]
    width = (warmed[-1][0] - warmed[0][0]) / 3
    windows = [[sample for sample in warmed if min(int((sample[0] - warmed[0][0]) / width), 2) == n] for n in range(3)]
    if not all(windows):
        return ["server resource sampling missed a growth window"]
    problems = []
    for index, name, limit in ((1, "threads", THREAD_GROWTH), (2, "RSS", RSS_GROWTH_KIB)):
        first, middle, last = [statistics.median(sample[index] for sample in window) for window in windows]
        if first < middle < last and last - first > limit:
            problems.append(
                f"server {name} kept growing: medians {first:g}, {middle:g}, {last:g}; growth budget {limit}"
            )
    return problems


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
        problems.append(f"{WRITE_BREACH} {worst:.3f}s exceeds {WRITE_P95_S}s")
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
        [sys.executable, ledger_server.__file__, "--serve"],
        env=env,
        stdout=log,
        stderr=subprocess.PIPE,
        stdin=subprocess.DEVNULL,
    )
    startup_stderr: collections.deque[bytes] = collections.deque(maxlen=STDERR_TAIL_LINES)
    relay = threading.Thread(target=relay_stderr, args=(server.stderr, log.fileno(), startup_stderr), daemon=True)
    relay.start()
    RELAYS[server.pid] = relay
    deadline = time.monotonic() + SERVER_WAIT_S
    while time.monotonic() < deadline:
        if server.poll() is not None:
            raise SystemExit(
                startup_failure(server, startup_stderr, f"exited with {server.returncode} before listening")
            )
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=1):
                return server
        except OSError:
            time.sleep(0.2)
    server.kill()
    server.wait()
    raise SystemExit(startup_failure(server, startup_stderr, f"did not listen on port {port} within {SERVER_WAIT_S}s"))


def startup_failure(server: subprocess.Popen, startup_stderr: collections.deque[bytes], outcome: str) -> str:
    RELAYS.pop(server.pid).join(timeout=RELAY_JOIN_S)
    return f"ledger server {outcome}; its stderr:\n" + b"".join(startup_stderr).decode(errors="replace")


def relay_stderr(pipe: BinaryIO, fd: int, tail: collections.deque[bytes]) -> None:
    for line in pipe:
        tail.append(line)
        view = memoryview(line)
        try:
            while view:
                view = view[os.write(fd, view) :]
        except OSError:
            pass


def stop_server(server: subprocess.Popen) -> None:
    server.terminate()
    try:
        server.wait(timeout=10)
    except subprocess.TimeoutExpired:
        server.kill()
        server.wait()
    if relay := RELAYS.pop(server.pid, None):
        relay.join(timeout=RELAY_JOIN_S)


def client(api: ResourceClient, name: str, item: str, start: float, deadline: float, results: dict) -> None:
    for n in itertools.count():
        began = start + n * write_every()
        if began >= deadline:
            return
        time.sleep(max(0.0, began - time.monotonic()))
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


def reader(api: ResourceClient, items: list[str], start: float, deadline: float, results: dict) -> None:
    """One agent polling its own task, phase and follow up, READS_PER_SECOND single resource reads a second."""
    for n in itertools.count():
        began = start + n / READS_PER_SECOND
        if began >= deadline:
            return
        time.sleep(max(0.0, began - time.monotonic()))
        try:
            sent = time.monotonic()
            api.request(SLUG, items[n % len(items)])
            results["item_reads"].append(time.monotonic() - sent)
        except Exception as exc:  # every failure is a red request, whatever raised it
            results["errors"].append(f"{items[0]} reader: {type(exc).__name__}: {exc}")


def single_items(n: int) -> list[str]:
    return [f"tasks/t{n * (TASKS // CLIENTS)}", f"phases/p{n % PHASES}", f"followups/f-{n}"]


def watcher(base: str, credentials: dict, deadline: float, results: dict) -> None:
    """The operator's page: one event stream held open through the load, as the live page holds it."""
    request = urllib.request.Request(
        f"{base}/api/v1/ledgers/{SLUG}/events", headers={**credentials, "Accept": "text/event-stream"}
    )
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_S) as stream:
            while (remaining := deadline - time.monotonic()) > 0:
                stream.fp.raw._sock.settimeout(min(TIMEOUT_S, remaining))
                try:
                    chunk = stream.read1(65536)
                except TimeoutError:
                    if remaining <= TIMEOUT_S:
                        break
                    raise
                if not chunk and time.monotonic() < deadline:
                    raise ConnectionError("the server closed the stream before the load ended")
    except Exception as exc:  # a dropped or silent stream is a red request
        results["errors"].append(f"event stream: {type(exc).__name__}: {exc}")


def sample_cpu(pid: int, deadline: float, results: dict) -> None:
    try:
        while True:
            at = time.monotonic()
            results["cpu"].append((at, cpu_seconds(pid)))
            results["resources"].append((at, *server_resources(pid)))
            if at >= deadline:
                break
            time.sleep(1)
    except Exception as exc:
        results["errors"].append(f"server sampler: {type(exc).__name__}: {exc}")


def load(port: int, token: str, pid: int, seconds: float) -> dict:
    base = f"http://127.0.0.1:{port}"
    results = {"writes": [], "reads": [], "item_reads": [], "errors": [], "cpu": [], "resources": []}
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
    threads += [
        threading.Thread(
            target=reader, args=(api, single_items(n), began + n / CLIENTS / READS_PER_SECOND, deadline, results)
        )
        for n, (api, _, _) in enumerate(apis)
    ]
    threads.append(threading.Thread(target=watcher, args=(base, {"X-Ledger-Token": token}, deadline, results)))
    threads.append(threading.Thread(target=sample_cpu, args=(pid, deadline, results)))
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    return results


def run(folder: Path) -> tuple[list[str], float | None]:
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
    problems.extend(resource_verdict(results["resources"]))
    item_reads, expected_reads = results["item_reads"], CLIENTS * math.ceil(seconds * READS_PER_SECOND)
    if len(item_reads) < MIN_WRITES * expected_reads:
        problems.append(f"{len(item_reads)} single resource reads completed of {expected_reads} expected")
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
                "item_reads": len(item_reads),
                "item_read_p95_s": round(p95(item_reads), 3) if item_reads else None,
                "busiest_cores": None if cores is None else round(cores, 3),
                "errors": len(results["errors"]),
                "resources": results["resources"],
                "budget": {
                    "write_p95_s": WRITE_P95_S,
                    "cores": CPU_CORES,
                    "thread_growth": THREAD_GROWTH,
                    "rss_growth_kib": RSS_GROWTH_KIB,
                },
            }
        ),
        flush=True,
    )
    if problems:
        print(server_log[-4000:])
    return problems, p95_or_none(writes)


def measured(folder: Path) -> list[str]:
    problems, first = run(folder)
    if not problems or not all(problem.startswith(WRITE_BREACH) for problem in problems):
        return problems
    print(
        "::notice::write p95 alone broke its budget, so the gate repeated the measure once on a fresh ledger"
        " and graded the repeat, which passes only when the better of the two is within budget",
        flush=True,
    )
    repeat_problems, repeat = run(folder / "repeat")
    shown = "no writes" if repeat is None else f"{repeat:.3f}s"
    print(f"::notice::write p95 first {first:.3f}s, repeat {shown}, budget {WRITE_P95_S}s", flush=True)
    return repeat_problems


def budgets(source: str) -> dict:
    found = {
        node.targets[0].id: ast.literal_eval(node.value)
        for node in ast.parse(source).body
        if isinstance(node, ast.Assign)
        and len(node.targets) == 1
        and isinstance(node.targets[0], ast.Name)
        and node.targets[0].id in BUDGETS
    }
    legacy = {"RESOURCE_WARMUP_S": 20, "RESOURCE_WINDOW_S": 60, "THREAD_GROWTH": 8, "RSS_GROWTH_KIB": 65_536}
    if not set(legacy).intersection(found):
        found.update(legacy)
    if missing := sorted(set(BUDGETS) - set(found)):
        sys.exit(f"the base gate assigns no {', '.join(missing)}")
    return found


def base_budgets(revision: str) -> dict:
    gate = Path(__file__)
    # Repository relative, so a mutation sandbox copy inside the checkout still reads the tracked gate.
    show = subprocess.run(
        ["git", "show", f"{revision}:tests/ledger_load/{gate.name}"], cwd=gate.parent, capture_output=True, text=True
    )
    if show.returncode:
        sys.exit(f"git could not show the ledger load gate at base {revision}: {show.stderr.strip()}")
    return budgets(show.stdout)


def graded(base: dict) -> dict:
    return {name: pick(globals()[name], base[name]) for name, pick in BUDGETS.items()}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--folder", type=Path, required=True, help="an empty scratch ledger folder")
    parser.add_argument("--base", required=True, help="the revision whose budgets grade this run")
    args = parser.parse_args(argv)
    if os.environ.get("GITHUB_ACTIONS") != "true":
        sys.exit("the ledger load gate runs in CI only: it loads the machine for a minute and a half")
    globals().update(graded(base_budgets(args.base)))
    args.folder.mkdir(parents=True, exist_ok=True)
    problems = measured(args.folder)
    for problem in problems:
        print(f"::error::{problem}")
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
