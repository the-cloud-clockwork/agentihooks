import json
import multiprocessing
import os
import time
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
from pathlib import Path

from scripts.swarm_ledger.repository.sqlite import SQLiteLedgerRepository


def document():
    tasks = [
        {
            "id": f"task-{index}",
            "title": f"Task {index}",
            "state": "done",
            "description": "recorded request " * 20,
            "comments": [
                {"id": f"comment-{comment}", "text": "recorded evidence " * 10, "by": "engineer"}
                for comment in range(10)
            ],
        }
        for index in range(1600)
    ]
    seed = {"title": "Replay", "tasks": tasks[:500], "phases": [], "notifications": []}
    return {
        **seed,
        "tasks": tasks,
        "_meta": {"rev": 1, "stamps": {}, "members": {}, "events": [], "seeds": {"0": seed, "1": seed}},
    }


def serve(folder, pipe):
    from scripts.swarm_ledger import ledger_server

    ledger_server.core.LEDGER_DIR = folder
    ledger_server.stored._path = folder / "ledger.sqlite3"
    server = ThreadingHTTPServer(("127.0.0.1", 0), ledger_server.Handler)
    port = server.server_address[1]
    ledger_server.ALLOWED_HOSTS = {f"127.0.0.1:{port}"}
    pipe.send(port)
    pipe.close()
    server.serve_forever()


def rss(pid):
    for line in Path(f"/proc/{pid}/status").read_text().splitlines():
        if line.startswith("VmRSS:"):
            return int(line.split()[1])
    raise AssertionError("server RSS absent")


def request(port, token, method, resource):
    connection = HTTPConnection("127.0.0.1", port, timeout=30)
    try:
        connection.request(
            method,
            f"/api/v1/ledgers/replay/{resource}",
            "{}" if method == "POST" else None,
            {"X-Ledger-Token": token, "Content-Type": "application/json"},
        )
        response = connection.getresponse()
        body = json.loads(response.read())
        assert response.status == 200, body
        return body
    finally:
        connection.close()


def test_server_memory_replay(tmp_path):
    repository = SQLiteLedgerRepository(tmp_path / "ledger.sqlite3")
    repository.import_document("replay", document())
    token = repository.token("replay")
    context = multiprocessing.get_context("spawn")
    receiver, sender = context.Pipe(duplex=False)
    server = context.Process(target=serve, args=(tmp_path, sender))
    server.start()
    sender.close()
    try:
        assert receiver.poll(30), "server did not start"
        port = receiver.recv()
        receiver.close()
        batches = int(os.environ.get("ROW_REPLAY_BATCHES", "2"))
        interval = float(os.environ.get("ROW_REPLAY_INTERVAL", "0"))
        started = time.monotonic()
        samples = [{"batch": 0, "rss_kib": rss(server.pid)}]
        for batch in range(batches):
            for _ in range(3):
                result = request(port, token, "GET", "tasks?limit=100")
                assert len(result["data"]) == 100
            result = request(port, token, "POST", "export")
            assert len(result["data"]["tasks"]) == 1600
            del result
            time.sleep(max(0, started + (batch + 1) * interval - time.monotonic()))
            samples.append({"batch": batch + 1, "rss_kib": rss(server.pid)})
        report = {
            "batches": batches,
            "requests": batches * 4,
            "elapsed_seconds": time.monotonic() - started,
            "rss_start_kib": samples[0]["rss_kib"],
            "rss_end_kib": samples[-1]["rss_kib"],
            "rss_peak_kib": max(sample["rss_kib"] for sample in samples),
            "samples": samples,
        }
        print("ROW_MEMORY " + json.dumps({key: value for key, value in report.items() if key != "samples"}))
        if destination := os.environ.get("ROW_MEMORY_REPORT"):
            Path(destination).write_text(json.dumps(report, indent=2) + "\n")
    finally:
        receiver.close()
        server.terminate()
        server.join(timeout=10)
        assert not server.is_alive()
