import importlib
import json
import threading
from collections import deque
from http.server import ThreadingHTTPServer

import pytest

from scripts.swarm_ledger import api, new_ledger
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
    repo.import_document(
        "timing",
        server.core.normalize(
            {
                **new_ledger.build_doc({"title": "Timing"}),
                "_meta": {"rev": 1, "stamps": {}, "events": [], "members": {}},
            }
        ),
    )
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


def test_command_names_the_cpu_hot_route_only_inside_its_window(monkeypatch, capsys, ledger_port):
    metrics = importlib.import_module("scripts.swarm_ledger.request_metrics")
    monkeypatch.setattr(metrics, "SAMPLES", deque(maxlen=metrics.MAX_SAMPLES))
    clock = Clock()
    monkeypatch.setattr(metrics, "time", clock)

    def read(handler, module):
        clock.wall += 3.0
        clock.cpu += 2.0

    monkeypatch.setattr(api, "handle", read)
    handler = server.Handler.__new__(server.Handler)
    handler.command = "GET"
    handler.path = "/api/v1/ledgers/earlier/tasks?cursor=private"
    handler.do_GET()
    clock.wall += 7200.0
    for slug in ("first", "second"):
        handler.path = f"/api/v1/ledgers/{slug}/export"
        handler.do_GET()
    monkeypatch.setattr(server, "ALLOWED_HOSTS", {f"127.0.0.1:{ledger_port}"})
    with ThreadingHTTPServer(("127.0.0.1", ledger_port), server.Handler) as http:
        thread = threading.Thread(target=http.serve_forever)
        thread.start()
        try:
            metrics.main(["--hours", "1", "--url", f"http://127.0.0.1:{ledger_port}"])
        finally:
            http.shutdown()
            thread.join()
    result = json.loads(capsys.readouterr().out)
    assert result["window_end"] - result["window_start"] == 3600.0
    assert result["writes"]["count"] == 0
    assert result["writes"]["p95_wall_seconds"] is None
    assert result["routes"] == [
        {"method": "GET", "route": "/api/v1/ledgers/:slug/export", "count": 2, "cpu_seconds": 4.0}
    ]


def test_bounded_snapshot_names_capacity_and_restart_gaps(monkeypatch):
    metrics = importlib.import_module("scripts.swarm_ledger.request_metrics")
    monkeypatch.setattr(metrics, "SAMPLES", deque(maxlen=3))
    clock = Clock()
    monkeypatch.setattr(metrics, "time", clock)
    monkeypatch.setattr(metrics, "STARTED_AT", clock.time() - 86400.0, raising=False)
    monkeypatch.setattr(metrics, "EVICTED_THROUGH", float("-inf"), raising=False)
    for wall in range(1, 21):
        clock.wall += 1.0
        metrics.record("POST", "/api/v1/ledgers/:slug/operations", float(wall), 0.25, 1.0)
    result = metrics.report(1.0)
    assert result["writes"]["count"] == 3
    assert result["writes"]["p95_wall_seconds"] == 20.0
    assert result["slow_days"] == [{"day": "2026-10-13", "count": 3}]
    assert result["coverage"]["complete"] is False
    assert result["coverage"]["evicted_through"] == 1791856817.0
    assert result["coverage"]["capacity"] == 3
    assert result["coverage"]["started_at"] == 1791770400.0
    clock.wall += 3601.0
    empty = metrics.report(1.0)
    assert empty["coverage"]["complete"] is True
    assert empty["writes"]["count"] == 0
    assert empty["writes"]["p95_wall_seconds"] is None
    monkeypatch.setattr(metrics, "STARTED_AT", clock.time())
    restarted = metrics.report(1.0)
    assert restarted["coverage"]["complete"] is False


@pytest.mark.parametrize(
    ("path", "route"),
    [
        ("/api/v1/ledgers/one/export", "/api/v1/ledgers/:slug/export"),
        ("/api/v1/ledgers/one", "/api/v1/ledgers/:slug/metadata"),
        ("/api/v1/ledgers", "/api/v1/ledgers"),
        ("/api/v1/ledgers/one/tasksX", "/api/v1/ledgers/:slug/:resource"),
        ("/api/v1/ledgers/one/tasks/item", "/api/v1/ledgers/:slug/tasks/:item"),
        ("/api/v1/ledgers/one/questions/item/answers", "/api/v1/ledgers/:slug/questions/:item/answers"),
        ("/api/v1/ledgers/one/tasks/item/workspace", "/api/v1/ledgers/:slug/tasks/:item/workspace"),
        ("/api/v1/ledgers/one/tasks/item/private", "/api/v1/ledgers/:slug/tasks/:item/:resource"),
        ("/api/swarm/private", "/api/swarm/:slug"),
        ("/api/v1/ledgers/one/export/?private=?hidden", "/api/v1/ledgers/:slug/export"),
        ("/api/v1/ledgers/one/tasks/item/comments", "/api/v1/ledgers/:slug/tasks/:item/comments"),
        ("/api/v1/ledgers/private/custom-private?cursor=private", "/api/v1/ledgers/:slug/:resource"),
        ("/api/swarm/private/models", "/api/swarm/:slug/models"),
        ("/api/private", "/api/:slug"),
        ("/private.html", "/:slug"),
        ("/static/version/ledger.js", "/static/:asset"),
        ("/healthz", "/healthz"),
    ],
)
def test_route_cpu_labels_have_bounded_cardinality(path, route, monkeypatch):
    metrics = importlib.import_module("scripts.swarm_ledger.request_metrics")
    monkeypatch.setattr(metrics, "SAMPLES", deque(maxlen=metrics.MAX_SAMPLES))
    monkeypatch.setattr(metrics, "time", Clock())
    monkeypatch.setattr(api, "handle", lambda *args: None)
    handler = server.Handler.__new__(server.Handler)
    handler.path, handler.command = path, "GET"
    handler.refused = lambda *args: False
    handler.exists = lambda *args: False
    handler.get_swarm = lambda *args: None
    handler.send_static = lambda *args: None
    handler.send = lambda *args: None
    handler.do_GET()
    assert metrics.report(1.0)["routes"] == [{"method": "GET", "route": route, "count": 1, "cpu_seconds": 0.0}]


def test_failed_write_keeps_its_file_lock_wait(tmp_path, monkeypatch):
    from scripts.swarm_ledger.repository import legacy

    metrics = importlib.import_module("scripts.swarm_ledger.request_metrics")
    monkeypatch.setattr(metrics, "SAMPLES", deque(maxlen=metrics.MAX_SAMPLES))
    clock = Clock()
    monkeypatch.setattr(metrics, "time", clock)

    def flock(handle, operation):
        if operation == legacy.fcntl.LOCK_EX:
            clock.wall += 2.0

    def write(handler, module):
        with legacy.storage_lock(tmp_path):
            clock.wall += 6.0
            clock.cpu += 1.5
            raise ValueError("refused")

    monkeypatch.setattr(legacy.fcntl, "flock", flock)
    monkeypatch.setattr(api, "handle", write)
    handler = server.Handler.__new__(server.Handler)
    handler.path, handler.command = "/api/v1/ledgers/timing/operations", "POST"
    with pytest.raises(ValueError, match="refused"):
        handler.do_POST()
    result = metrics.report(1.0)
    assert result["writes"]["count"] == 1
    assert result["writes"]["p95_wall_seconds"] == 8.0
    assert result["writes"]["cpu_seconds"] == 1.5
    assert result["writes"]["lock_wait_seconds"] == 2.0
    assert result["writes"]["other_wall_seconds"] == 4.5
