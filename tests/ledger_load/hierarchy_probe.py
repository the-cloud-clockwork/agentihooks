import cProfile
import gzip
import io
import json
import os
import pstats
import queue
import threading
import time
import urllib.request
from pathlib import Path

import pytest

from scripts.swarm_ledger import ledger_server as server
from scripts.swarm_ledger import request_metrics
from scripts.swarm_ledger.events import Hub
from scripts.swarm_ledger.events.publishing import Publishing
from scripts.swarm_ledger.repository.sqlite import SQLiteLedgerRepository

pytestmark = pytest.mark.xdist_group("fakeredis")

SLUG = "hierarchy-cpu-proof"


def fetch(port, path, samples):
    url = f"http://127.0.0.1:{port}/api/v1/ledgers/{SLUG}/{path}"
    request = urllib.request.Request(url, headers={"X-Ledger-Token": "hierarchy-proof"})
    with urllib.request.urlopen(request, timeout=60) as response:
        body = response.read()
        assert response.status == 200
    used = samples.get(timeout=10)
    return body.decode(), used


def pages(port, path, samples):
    output, cursor = [], None
    for _ in range(2000):
        query = "?limit=100" + (f"&cursor={cursor}" if cursor else "")
        body, _ = fetch(port, path + query, samples)
        output.append(body)
        cursor = json.loads(body)["next_cursor"]
        if cursor is None:
            return output
    raise AssertionError("Hierarchy pagination did not terminate")


def observe(port, samples, state):
    cpu, responses = [], []
    for _ in range(8):
        body, used = fetch(port, "hierarchy", samples)
        cpu.append(used)
        responses.append(body)
    assert len(set(responses)) == 1
    outputs = {"hierarchy": pages(port, "hierarchy", samples)}
    phase = state["phases"][0]["id"]
    task = state["tasks"][0]["id"]
    for path in (
        "hierarchy/children",
        f"hierarchy/subtree/phases/{phase}",
        f"hierarchy/children/phases/{phase}",
        f"hierarchy/ancestors/tasks/{task}",
        f"hierarchy/dependents/tasks/{task}",
    ):
        outputs[path] = pages(port, path, samples)
    return {"cpu_seconds": cpu, "outputs": outputs}


def align(barrier):
    if not barrier:
        return
    folder = Path(barrier)
    folder.mkdir(parents=True, exist_ok=True)
    (folder / str(os.getpid())).touch()
    deadline = time.monotonic() + 300
    while len(list(folder.iterdir())) < 2:
        assert time.monotonic() < deadline, "the paired hierarchy probe never became ready"
        time.sleep(0.05)


def test_hierarchy_http_cpu_and_exact_responses(tmp_path, monkeypatch):
    assert Path(server.__file__).resolve().is_relative_to(Path.cwd().resolve())
    corpus = Path(__file__).parents[1] / "swarm_ledger" / "operations_corpus.json.gz"
    recorded = json.loads(gzip.decompress(corpus.read_bytes()))
    core = server.core
    monkeypatch.setattr(core, "LEDGER_DIR", tmp_path)
    monkeypatch.setattr("hooks._redis.get_redis", lambda: None)
    monkeypatch.setattr(server.Handler, "log_message", lambda *args: None)
    repo = SQLiteLedgerRepository(tmp_path / "ledger.sqlite3", domain=core)
    monkeypatch.setattr(server, "HUB", Hub())
    monkeypatch.setattr(server, "served_version", lambda: "hierarchy-proof")
    monkeypatch.setattr(server, "repository", Publishing(repo, server.publish_snapshot))
    samples = queue.Queue()
    monkeypatch.setattr(request_metrics, "record", lambda method, route, wall, cpu, lock: samples.put(cpu))
    profiles = []
    profiling = threading.Event()

    class ProfileHandler(server.Handler):
        def do_GET(self):
            if not profiling.is_set():
                return super().do_GET()
            profiler = cProfile.Profile(timer=time.thread_time)
            profiler.enable()
            try:
                return super().do_GET()
            finally:
                profiler.disable()
                stream = io.StringIO()
                pstats.Stats(profiler, stream=stream).sort_stats("cumulative").print_stats(35)
                profiles.append(stream.getvalue())

    httpd = server.ThreadingHTTPServer(("127.0.0.1", 0), ProfileHandler)
    port = httpd.server_port
    monkeypatch.setattr(server, "ALLOWED_HOSTS", {f"127.0.0.1:{port}"})
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    observations = []
    try:
        state = recorded["state"]
        repo.import_document(SLUG, state, token="hierarchy-proof")
        server.HUB.open(SLUG, lambda: {"ledger": server.ledger_view(state)})
        align(os.environ.get("HIERARCHY_START_BARRIER"))
        observations.append(observe(port, samples, state))
        profiling.set()
        fetch(port, "hierarchy", samples)
        profiling.clear()
        changed = json.loads(json.dumps(state))
        changed["tasks"][0]["state"] = "claimed"
        changed["phases"][0]["done"] = not bool(changed["phases"][0].get("done"))
        repo.import_document(SLUG, changed, token="hierarchy-proof", replace=True)
        observations.append(observe(port, samples, changed))
        assert observations[0]["outputs"] != observations[1]["outputs"]
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=10)
    seconds = [value for observation in observations for value in observation["cpu_seconds"]]
    report = {
        "source": recorded["source"],
        "requests": len(seconds),
        "mean_cpu_seconds": sum(seconds) / len(seconds),
        "observations": observations,
        "profile": profiles[0],
    }
    Path(os.environ["HIERARCHY_CPU_REPORT"]).write_text(json.dumps(report))
    print(json.dumps({key: value for key, value in report.items() if key != "observations"}, indent=2))
