import importlib
import json
import threading
from collections import deque
from http.server import ThreadingHTTPServer

from scripts.swarm_ledger import api
from scripts.swarm_ledger import ledger_server as server
from scripts.swarm_ledger.repository.sqlite import SQLiteLedgerRepository


class Clock:
    def __init__(self):
        self.wall = 0.0
        self.cpu = 0.0

    def perf_counter(self):
        return self.wall

    def thread_time(self):
        return self.cpu

    def time(self):
        return 1791856800.0 + self.wall


class DelayedLock:
    def __init__(self, clock):
        self.clock = clock

    def acquire(self):
        self.clock.wall += 2.0

    def release(self):
        pass

    def __enter__(self):
        self.acquire()

    def __exit__(self, *args):
        self.release()


def test_server_write_command_reports_cpu_lock_wait_and_remaining_wall(tmp_path, monkeypatch, capsys, ledger_port):
    metrics = importlib.import_module("scripts.swarm_ledger.request_metrics")
    monkeypatch.setenv("LEDGER_DIR", str(tmp_path))
    monkeypatch.setattr(metrics, "SAMPLES", deque(maxlen=metrics.MAX_SAMPLES))
    clock = Clock()
    monkeypatch.setattr(metrics, "time", clock)
    repo = SQLiteLedgerRepository(tmp_path / "ledger.sqlite3", domain=server.core)
    repo.import_document("timing", {"title": "Timing", "_meta": {"rev": 1, "stamps": {}, "events": [], "members": {}}})
    monkeypatch.setattr(server.core, "LOCK", DelayedLock(clock))

    def write(handler, module):
        repo.apply_ops("timing")
        clock.wall += 6.0
        clock.cpu += 1.5

    monkeypatch.setattr(api, "handle", write)
    handler = server.Handler.__new__(server.Handler)
    handler.path = "/api/v1/ledgers/timing/operations?private=hidden"
    handler.command = "POST"
    handler.do_POST()
    monkeypatch.setattr(server, "ALLOWED_HOSTS", {f"127.0.0.1:{ledger_port}"})
    with ThreadingHTTPServer(("127.0.0.1", ledger_port), server.Handler) as http:
        thread = threading.Thread(target=http.serve_forever)
        thread.start()
        try:
            metrics.main(["--hours", "24", "--url", f"http://127.0.0.1:{ledger_port}"])
        finally:
            http.shutdown()
            thread.join()
    report = json.loads(capsys.readouterr().out)
    assert report["writes"]["count"] == 1
    assert report["writes"]["p95_wall_seconds"] == 8.0
    assert report["writes"]["cpu_seconds"] == 1.5
    assert report["writes"]["lock_wait_seconds"] == 2.0
    assert report["writes"]["other_wall_seconds"] == 4.5
    assert report["slow_days"] == [{"day": "2026-10-13", "count": 1}]
    assert report["routes"] == [
        {"method": "POST", "route": "/api/v1/ledgers/:slug/operations", "count": 1, "cpu_seconds": 1.5}
    ]
    assert "timing" not in json.dumps(report)
    assert "hidden" not in json.dumps(report)
