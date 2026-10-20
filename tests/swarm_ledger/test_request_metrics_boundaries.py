import io
import json
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from http.server import ThreadingHTTPServer
from types import SimpleNamespace
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

from scripts.swarm_ledger import api
from scripts.swarm_ledger import ledger_server as server
from scripts.swarm_ledger import request_metrics as metrics


@pytest.fixture
def capture(monkeypatch):
    now = [1791856800.0]
    monkeypatch.setattr(metrics, "SAMPLES", deque(maxlen=64))
    monkeypatch.setattr(metrics, "STARTED_AT", now[0] - metrics.RETENTION_SECONDS)
    monkeypatch.setattr(metrics, "EVICTED_THROUGH", float("-inf"))
    monkeypatch.setattr(
        metrics,
        "time",
        SimpleNamespace(time=lambda: now[0], perf_counter=time.perf_counter, thread_time=time.thread_time),
    )
    return now


@pytest.fixture
def metrics_url(monkeypatch, ledger_port):
    monkeypatch.setattr(server, "ALLOWED_HOSTS", {f"127.0.0.1:{ledger_port}"})
    with ThreadingHTTPServer(("127.0.0.1", ledger_port), server.Handler) as http:
        thread = threading.Thread(target=http.serve_forever)
        thread.start()
        try:
            yield f"http://127.0.0.1:{ledger_port}"
        finally:
            http.shutdown()
            thread.join()


def test_p95_counts_all_write_methods_and_excludes_reads(capture):
    for wall in reversed(range(1, 41)):
        method = ("POST", "PUT", "PATCH", "DELETE")[wall % 4]
        metrics.record(method, "/api/v1/ledgers/:slug/tasks", float(wall), 0.5, 0.25)
    metrics.record("GET", "/api/v1/ledgers/:slug/tasks", 999.0, 50.0, 0.0)
    result = metrics.report(1.0)
    assert result["writes"] == {
        "count": 40,
        "p95_wall_seconds": 38.0,
        "cpu_seconds": 20.0,
        "lock_wait_seconds": 10.0,
        "other_wall_seconds": 790.0,
    }
    assert result["slow_days"] == [{"day": "2026-10-13", "count": 36}]
    assert result["routes"][0] == {
        "method": "GET",
        "route": "/api/v1/ledgers/:slug/tasks",
        "count": 1,
        "cpu_seconds": 50.0,
    }
    assert [row["method"] for row in result["routes"][1:]] == ["DELETE", "PATCH", "POST", "PUT"]


def test_slow_days_use_utc_and_residual_never_goes_negative(capture, monkeypatch):
    monkeypatch.setenv("TZ", "Pacific/Honolulu")
    time.tzset()
    try:
        capture[0] = datetime(2026, 10, 12, 23, 59, 59, tzinfo=UTC).timestamp()
        metrics.record("POST", "/api/v1/ledgers/:slug/tasks", 5.0, 4.0, 2.0)
        capture[0] += 2
        metrics.record("PUT", "/api/v1/ledgers/:slug/tasks", 4.999, 1.0, 0.0)
        metrics.record("PATCH", "/api/v1/ledgers/:slug/tasks", 5.0, 1.0, 0.0)
        metrics.record("DELETE", "/api/v1/ledgers/:slug/tasks", 5.0, 1.0, 0.0)
        result = metrics.report(1.0)
        assert result["slow_days"] == [{"day": "2026-10-12", "count": 1}, {"day": "2026-10-13", "count": 2}]
        assert result["writes"]["other_wall_seconds"] == pytest.approx(11.999)
    finally:
        monkeypatch.undo()
        time.tzset()


def test_window_includes_both_edges_and_marks_eviction_at_start(capture, monkeypatch):
    end = capture[0]
    for ended, cpu in ((end - 3600.001, 100.0), (end - 3600, 1.0), (end, 2.0), (end + 0.001, 200.0)):
        capture[0] = ended
        metrics.record("POST", "/api/v1/ledgers/:slug/tasks", 6.0, cpu, 0.0)
    capture[0] = end
    monkeypatch.setattr(metrics, "STARTED_AT", end - 3600)
    result = metrics.report(1.0)
    assert result["writes"]["count"] == 2
    assert result["writes"]["cpu_seconds"] == 3.0
    assert result["coverage"]["complete"] is True
    assert result["coverage"]["evicted_through"] is None
    monkeypatch.setattr(metrics, "EVICTED_THROUGH", end - 3600)
    assert metrics.report(1.0)["coverage"]["complete"] is False


def test_age_retention_keeps_exact_edge_and_reports_eviction(capture):
    first = capture[0]
    metrics.record("POST", "/api/v1/ledgers/:slug/tasks", 8.0, 1.0, 2.0)
    capture[0] += metrics.RETENTION_SECONDS
    metrics.record("GET", "/healthz", 0.1, 0.01, 0.0)
    assert metrics.report(168.0)["writes"]["count"] == 1
    capture[0] += 0.001
    metrics.record("GET", "/healthz", 0.1, 0.01, 0.0)
    result = metrics.report(168.0)
    assert result["writes"]["count"] == 0
    assert result["coverage"]["evicted_through"] == first
    assert result["routes"] == [{"method": "GET", "route": "/healthz", "count": 2, "cpu_seconds": 0.02}]


@pytest.mark.parametrize("hours", ["0", "-1", "169", "nan", "inf", "-inf", "garbage", ""])
def test_endpoint_rejects_invalid_windows(capture, metrics_url, hours):
    with pytest.raises(HTTPError) as caught:
        urlopen(f"{metrics_url}/api/v1/metrics?hours={hours}", timeout=10)
    assert caught.value.code == 400
    assert json.load(caught.value) == {"error": {"code": "invalid_window", "message": "hours out of range"}}


@pytest.mark.parametrize(("query", "hours"), [("", 24), ("?hours=168", 168), ("?hours=0.25", 0.25)])
def test_endpoint_accepts_default_maximum_and_fractional_windows(capture, metrics_url, query, hours):
    with urlopen(f"{metrics_url}/api/v1/metrics{query}", timeout=10) as response:
        assert response.status == 200
        result = json.load(response)
    assert result["window_start"] == capture[0] - hours * 3600
    assert result["window_end"] == capture[0]
    assert result["writes"]["count"] == 0


@pytest.mark.parametrize("hours", ["0", "-1", "169", "nan", "inf", "-inf"])
def test_command_rejects_invalid_windows_before_http(capture, monkeypatch, capsys, hours):
    calls = []
    monkeypatch.setattr(metrics, "urlopen", lambda *args, **kwargs: calls.append((args, kwargs)))
    with pytest.raises(SystemExit) as caught:
        metrics.main([f"--hours={hours}"])
    assert caught.value.code == 2
    assert capsys.readouterr().err.splitlines()[-1].endswith(": error: hours must be positive and at most seven days")
    assert calls == []


@pytest.mark.parametrize(
    ("args", "hours", "base"),
    [
        ([], 24.0, "http://example:9999"),
        (["--hours", "168", "--url", "http://example:9999/"], 168.0, "http://example:9999"),
        (["--url", "http://example:9999/proxyX/"], 24.0, "http://example:9999/proxyX"),
    ],
)
def test_command_defaults_trims_url_and_emits_sorted_json(capture, monkeypatch, capsys, args, hours, base):
    from scripts.swarm_ledger import ledger_link

    calls = []

    def read(url, timeout):
        calls.append((url, timeout))
        return io.StringIO('{"z": 2, "a": 1}')

    monkeypatch.setattr(ledger_link, "address", lambda environ: ("example", 9999))
    monkeypatch.setattr(metrics, "urlopen", read)
    metrics.main(args)
    assert calls == [(f"{base}/api/v1/metrics?hours={hours}", 10)]
    assert capsys.readouterr().out == '{"a": 1, "z": 2}\n'


def test_parallel_handlers_keep_request_clocks_and_lock_wait_separate(capture, monkeypatch):
    local = threading.local()
    barrier = threading.Barrier(8)
    monkeypatch.setattr(
        metrics,
        "time",
        SimpleNamespace(time=lambda: capture[0], perf_counter=lambda: local.wall, thread_time=lambda: local.cpu),
    )

    def write(handler, module):
        barrier.wait(timeout=10)
        with metrics.waiting():
            local.wall += local.wait
        local.wall += 6.0
        local.cpu += 1.5

    monkeypatch.setattr(api, "handle", write)

    def request(index):
        local.wall, local.cpu, local.wait = 0.0, 0.0, float(index)
        handler = server.Handler.__new__(server.Handler)
        handler.path, handler.command = f"/api/v1/ledgers/private{index}/operations", "POST"
        handler.do_POST()
        assert metrics.CURRENT.get() is None

    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = [pool.submit(request, index) for index in range(8)]
        while not all(future.done() for future in futures):
            result = metrics.report(1.0)
            assert result["writes"]["cpu_seconds"] == result["writes"]["count"] * 1.5
            time.sleep(0.001)
        for future in futures:
            future.result()
    result = metrics.report(1.0)
    assert result["writes"] == {
        "count": 8,
        "p95_wall_seconds": 13.0,
        "cpu_seconds": 12.0,
        "lock_wait_seconds": 28.0,
        "other_wall_seconds": 36.0,
    }
    assert result["routes"] == [
        {"method": "POST", "route": "/api/v1/ledgers/:slug/operations", "count": 8, "cpu_seconds": 12.0}
    ]


@pytest.mark.parametrize(
    ("method", "label"),
    [("HEAD", "HEAD"), ("DELETE", "DELETE"), ("TRACE", "TRACE"), ("CONNECT", "CONNECT"), ("PRIVATE", "OTHER")],
)
def test_rejected_http_methods_record_cpu_without_arbitrary_method_labels(
    capture, monkeypatch, metrics_url, method, label
):
    completed = threading.Event()
    record = metrics.record

    def recorded(*args):
        record(*args)
        completed.set()

    monkeypatch.setattr(metrics, "record", recorded)
    request = Request(f"{metrics_url}/api/v1/ledgers/private/tasks", method=method)
    with pytest.raises(HTTPError) as caught:
        urlopen(request, timeout=10)
    assert caught.value.code == 501
    caught.value.close()
    assert completed.wait(5)
    result = metrics.report(1.0)
    assert len(result["routes"]) == 1
    assert result["routes"][0]["method"] == label
    assert result["routes"][0]["route"] == "/api/v1/ledgers/:slug/tasks"
    assert result["routes"][0]["count"] == 1
    assert result["routes"][0]["cpu_seconds"] > 0


def test_command_help_describes_the_timing_report(capsys):
    with pytest.raises(SystemExit) as caught:
        metrics.main(["--help"])
    assert caught.value.code == 0
    assert "Ledger write timing and request route CPU" in capsys.readouterr().out.splitlines()


@pytest.mark.parametrize("method", ["GET", "POST", "PUT", "PATCH", "OPTIONS"])
def test_supported_methods_keep_separate_route_cpu_totals(capture, monkeypatch, method):
    monkeypatch.setattr(api, "handle", lambda *args: None)
    handler = server.Handler.__new__(server.Handler)
    handler.command, handler.path, handler.headers = method, "/api/v1/ledgers/private/tasks", {}
    handler.send_response = handler.send_header = handler.end_headers = lambda *args: None
    getattr(handler, f"do_{method}")()
    result = metrics.report(1.0)
    assert len(result["routes"]) == 1
    assert result["routes"][0]["method"] == method
    assert result["routes"][0]["count"] == 1


def test_supported_http_request_is_recorded_once(capture, monkeypatch, metrics_url):
    completed = threading.Event()
    record = metrics.record

    def recorded(*args):
        record(*args)
        completed.set()

    monkeypatch.setattr(metrics, "record", recorded)
    with urlopen(f"{metrics_url}/api/v1/metrics", timeout=10) as response:
        json.load(response)
    assert completed.wait(5)
    result = metrics.report(1.0)
    assert len(result["routes"]) == 1
    assert result["routes"][0]["method"] == "GET"
    assert result["routes"][0]["count"] == 1


def test_empty_connection_records_no_request(capture, monkeypatch, metrics_url):
    import socket
    from urllib.parse import urlsplit

    completed = threading.Event()
    handle = server.Handler.handle_one_request

    def finished(handler):
        try:
            return handle(handler)
        finally:
            completed.set()

    monkeypatch.setattr(server.Handler, "handle_one_request", finished)
    address = urlsplit(metrics_url)
    with socket.create_connection((address.hostname, address.port), timeout=10):
        pass
    assert completed.wait(5)
    result = metrics.report(1.0)
    assert result["routes"] == []
    assert result["writes"]["count"] == 0
