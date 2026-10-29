import io
import json
import socket
import threading
import time
from collections import deque
from http.server import ThreadingHTTPServer
from types import SimpleNamespace

import pytest

from scripts.swarm_ledger import ledger_server as server
from scripts.swarm_ledger import request_body
from scripts.swarm_ledger import request_metrics as metrics
from scripts.swarm_ledger.repository import postgres

OPERATIONS = "/api/v1/ledgers/:slug/operations"


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


@pytest.fixture
def clock(monkeypatch):
    clock = Clock()
    monkeypatch.setattr(metrics, "SAMPLES", deque(maxlen=64))
    monkeypatch.setattr(metrics, "STARTED_AT", clock.time() - metrics.RETENTION_SECONDS)
    monkeypatch.setattr(metrics, "EVICTED_THROUGH", float("-inf"))
    monkeypatch.setattr(metrics, "time", clock)
    return clock


def write_handler():
    return SimpleNamespace(command="POST", path="/api/v1/ledgers/timing/operations", raw_requestline=b"POST")


def test_a_write_splits_client_postgres_and_lock_waits_out_of_other_wall(clock):
    def handle_one_request(handler):
        clock.wall += 3.0
        metrics.arrived()
        with metrics.client():
            clock.wall += 1.0
        with metrics.postgres():
            clock.wall += 2.0
        with metrics.waiting(), metrics.postgres():
            clock.wall += 4.0
        clock.wall += 0.5
        clock.cpu += 0.5

    metrics.measured(handle_one_request)(write_handler())
    writes = metrics.report(1.0)["writes"]
    assert writes == {
        "count": 1,
        "p95_wall_seconds": 10.5,
        "cpu_seconds": 0.5,
        "lock_wait_seconds": 4.0,
        "client_wait_seconds": 4.0,
        "postgres_wait_seconds": 2.0,
        "other_wall_seconds": 0.0,
    }
    [record] = metrics.records(1.0, None, 10)["routes"][0]["records"]
    assert record["client_wait_seconds"] == 4.0
    assert record["postgres_wait_seconds"] == 2.0


def test_waits_outside_a_measured_request_are_not_counted(clock):
    metrics.arrived()
    with metrics.client(), metrics.postgres():
        clock.wall += 1.0
    assert list(metrics.SAMPLES) == []


def test_body_reads_count_as_client_wait(clock):
    class Slow(io.BytesIO):
        def read(self, size=-1):
            clock.wall += 2.0
            return super().read(size)

    def handle_one_request(handler):
        assert request_body.Body(Slow(b"abcd"), 4).read() == b"abcd"
        assert request_body.Body(Slow(b"ef"), None).read() == b"ef"

    metrics.measured(handle_one_request)(write_handler())
    assert metrics.report(1.0)["writes"]["client_wait_seconds"] == 4.0


def test_postgres_stream_counts_only_its_fetches(clock):
    class Cursor:
        def __init__(self):
            self.batches = [[(1,), (2,)], []]

        def execute(self, sql, parameters):
            clock.wall += 1.0

        def fetchmany(self, size):
            clock.wall += 0.5
            return self.batches.pop(0)

    connection = postgres.Connection.__new__(postgres.Connection)
    connection.raw = SimpleNamespace(cursor=lambda name=None: cursor)
    cursor = Cursor()

    def handle_one_request(handler):
        for _row in connection.stream("SELECT 1"):
            clock.wall += 10.0

    metrics.measured(handle_one_request)(write_handler())
    assert metrics.report(1.0)["writes"]["postgres_wait_seconds"] == 2.0


def test_a_legacy_journal_sample_reads_with_zero_new_waits(tmp_path, monkeypatch, clock):
    path = tmp_path / "metrics.jsonl"
    ended = clock.time() - 10.0
    path.write_text(
        json.dumps(["held", ended - 100.0])
        + "\n"
        + json.dumps(["start", ended - 100.0])
        + "\n"
        + json.dumps(["s", ended, "POST", OPERATIONS, 1.0, 0.25, 0.5])
        + "\n"
        + json.dumps(["stop", ended + 1.0])
        + "\n"
    )
    monkeypatch.setattr(metrics, "JOURNAL", None)
    monkeypatch.setattr(metrics, "HELD_SINCE", None)
    monkeypatch.setattr(metrics, "LOST", [])
    metrics.restore(path)
    [record] = metrics.records(1.0, None, 10)["routes"][0]["records"]
    assert record == {
        "ended": ended,
        "wall_seconds": 1.0,
        "cpu_seconds": 0.25,
        "lock_wait_seconds": 0.5,
        "client_wait_seconds": 0.0,
        "postgres_wait_seconds": 0.0,
    }


def test_a_journal_sample_of_unknown_width_is_dropped():
    assert metrics.journal_row(["s", 1.0, "POST", OPERATIONS, 1.0, 0.0, 0.0, 0.0, 0.0])
    assert not metrics.journal_row(["s", 1.0, "POST", OPERATIONS, 1.0, 0.0, 0.0, 0.0])
    assert not metrics.journal_row(["held", 1.0, 2.0])


def test_a_client_slow_to_send_its_request_line_shows_as_client_wait(monkeypatch, ledger_port):
    monkeypatch.setattr(metrics, "SAMPLES", deque(maxlen=64))
    monkeypatch.setattr(server, "ALLOWED_HOSTS", {f"127.0.0.1:{ledger_port}"})
    timed = threading.Event()

    class Timing(metrics.Timing):
        def __post_init__(self):
            timed.set()

    monkeypatch.setattr(metrics, "Timing", Timing)
    with ThreadingHTTPServer(("127.0.0.1", ledger_port), server.Handler) as http:
        thread = threading.Thread(target=http.serve_forever)
        thread.start()
        try:
            with socket.create_connection(("127.0.0.1", ledger_port), timeout=10) as sock:
                assert timed.wait(10)
                time.sleep(0.5)
                sock.sendall(
                    b"POST /api/v1/ledgers/timing/operations HTTP/1.0\r\n"
                    + f"Host: 127.0.0.1:{ledger_port}\r\nContent-Length: 2\r\n\r\n{{}}".encode()
                )
                while sock.recv(65536):
                    pass
        finally:
            http.shutdown()
            thread.join()
    [row] = [row for row in metrics.SAMPLES if row[2] == OPERATIONS]
    assert row[6] >= 0.5
    assert row[3] >= row[6]
