import io
import json
import threading
import time
from collections import deque
from http.server import ThreadingHTTPServer
from types import SimpleNamespace
from urllib.error import HTTPError
from urllib.request import urlopen

import pytest

from scripts.swarm_ledger import ledger_server as server
from scripts.swarm_ledger import request_metrics

OPERATIONS = "/api/v1/ledgers/:slug/operations"
TASKS = "/api/v1/ledgers/:slug/tasks"
START = 1791856800.0


@pytest.fixture
def now(monkeypatch):
    clock = [START]
    monkeypatch.setattr(
        request_metrics,
        "time",
        SimpleNamespace(time=lambda: clock[0], perf_counter=time.perf_counter, thread_time=time.thread_time),
    )
    fresh(monkeypatch, clock[0] - 3600.0)
    return clock


def fresh(monkeypatch, started_at):
    monkeypatch.setattr(request_metrics, "SAMPLES", deque(maxlen=64))
    monkeypatch.setattr(request_metrics, "PENDING", [])
    monkeypatch.setattr(request_metrics, "STARTED_AT", started_at)
    monkeypatch.setattr(request_metrics, "EVICTED_THROUGH", float("-inf"))
    monkeypatch.setattr(request_metrics, "HELD_SINCE", None)
    monkeypatch.setattr(request_metrics, "LOST", [])
    monkeypatch.setattr(request_metrics, "JOURNAL", None)
    monkeypatch.setattr(request_metrics, "APPENDED", 0)


def reload(monkeypatch, clock, path, after):
    clock[0] += after
    fresh(monkeypatch, clock[0])
    request_metrics.restore(path)


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


def test_reader_groups_window_write_records_by_route(now):
    request_metrics.record("POST", OPERATIONS, 9.0, 1.0, 1.0)
    now[0] += 1800.0
    request_metrics.record("POST", OPERATIONS, 1.5, 0.5, 0.25)
    now[0] += 10.0
    request_metrics.record("PATCH", TASKS, 2.0, 0.75, 0.5)
    request_metrics.record("GET", TASKS, 50.0, 5.0, 0.0)
    now[0] += 10.0
    request_metrics.record("POST", OPERATIONS, 3.0, 1.25, 1.0)
    now[0] += 1800.0
    result = request_metrics.records(1.0, end=START + 3610.0, limit=100)
    assert result["window_start"] == START + 10.0
    assert result["window_end"] == START + 3610.0
    assert result["truncated"] is False
    assert result["coverage"]["complete"] is True
    assert result["coverage"]["lost"] == []
    assert result["routes"] == [
        {
            "method": "POST",
            "route": OPERATIONS,
            "count": 2,
            "records": [
                {"ended": START + 1800.0, "wall_seconds": 1.5, "cpu_seconds": 0.5, "lock_wait_seconds": 0.25},
                {"ended": START + 1820.0, "wall_seconds": 3.0, "cpu_seconds": 1.25, "lock_wait_seconds": 1.0},
            ],
        },
        {
            "method": "PATCH",
            "route": TASKS,
            "count": 1,
            "records": [
                {"ended": START + 1810.0, "wall_seconds": 2.0, "cpu_seconds": 0.75, "lock_wait_seconds": 0.5},
            ],
        },
    ]


def test_reader_keeps_the_newest_records_when_its_bound_is_reached(now):
    for wall in range(1, 6):
        now[0] += 1.0
        request_metrics.record("POST", OPERATIONS, float(wall), 0.0, 0.0)
    result = request_metrics.records(1.0, None, limit=2)
    assert result["truncated"] is True
    assert result["matched"] == 5
    assert [row["wall_seconds"] for row in result["routes"][0]["records"]] == [4.0, 5.0]


def test_clean_reload_keeps_every_record_readable(tmp_path, monkeypatch, now):
    path = tmp_path / "metrics.jsonl"
    request_metrics.restore(path)
    request_metrics.record("POST", OPERATIONS, 1.78, 0.5, 0.75)
    now[0] += 60.0
    request_metrics.record("DELETE", TASKS, 1.12, 0.25, 0.5)
    request_metrics.persist(stopped=True)
    reload(monkeypatch, now, path, 5.0)
    result = request_metrics.records(1.0, None, limit=100)
    assert result["coverage"]["complete"] is True
    assert result["coverage"]["lost"] == []
    assert result["coverage"]["held_since"] == START - 3600.0
    assert [(row["method"], row["count"]) for row in result["routes"]] == [("POST", 1), ("DELETE", 1)]
    assert result["routes"][0]["records"][0] == {
        "ended": START,
        "wall_seconds": 1.78,
        "cpu_seconds": 0.5,
        "lock_wait_seconds": 0.75,
    }


def test_crash_names_the_exact_unflushed_span(tmp_path, monkeypatch, now):
    path = tmp_path / "metrics.jsonl"
    request_metrics.restore(path)
    request_metrics.record("POST", OPERATIONS, 1.0, 0.0, 0.0)
    now[0] += 2.0
    request_metrics.persist()
    flushed = now[0]
    now[0] += 1.0
    request_metrics.record("POST", OPERATIONS, 2.0, 0.0, 0.0)
    reload(monkeypatch, now, path, 30.0)
    restarted = now[0]
    result = request_metrics.records(1.0, None, limit=100)
    assert result["coverage"]["complete"] is False
    assert result["coverage"]["lost"] == [{"start": flushed, "end": restarted}]
    assert [row["wall_seconds"] for row in result["routes"][0]["records"]] == [1.0]
    request_metrics.record("POST", OPERATIONS, 3.0, 0.0, 0.0)
    request_metrics.persist(stopped=True)
    reload(monkeypatch, now, path, 1.0)
    again = request_metrics.records(1.0, None, limit=100)
    assert again["coverage"]["lost"] == [{"start": flushed, "end": restarted}]
    assert [row["wall_seconds"] for row in again["routes"][0]["records"]] == [1.0, 3.0]
    now[0] += 3600.0
    later = request_metrics.records(0.5, None, limit=100)
    assert later["coverage"]["complete"] is True
    assert later["coverage"]["lost"] == []


def test_restore_drops_records_past_retention_and_compacts(tmp_path, monkeypatch, now):
    path = tmp_path / "metrics.jsonl"
    request_metrics.restore(path)
    request_metrics.record("POST", OPERATIONS, 1.0, 0.0, 0.0)
    request_metrics.persist(stopped=True)
    reload(monkeypatch, now, path, request_metrics.RETENTION_SECONDS + 10.0)
    request_metrics.record("POST", OPERATIONS, 2.0, 0.0, 0.0)
    held = list(request_metrics.SAMPLES)
    assert [row[3] for row in held] == [2.0]
    assert request_metrics.report(1.0)["coverage"]["held_since"] == now[0] - request_metrics.RETENTION_SECONDS
    assert len(path.read_text().splitlines()) == 2


def test_persist_without_restore_writes_nothing(tmp_path, monkeypatch, now):
    monkeypatch.chdir(tmp_path)
    request_metrics.record("POST", OPERATIONS, 1.0, 0.0, 0.0)
    request_metrics.persist(stopped=True)
    assert request_metrics.PENDING == []
    assert list(tmp_path.rglob("*.jsonl*")) == []


def test_journal_rewrites_when_appends_exceed_capacity(tmp_path, monkeypatch, now):
    path = tmp_path / "metrics.jsonl"
    request_metrics.restore(path)
    for wall in range(200):
        now[0] += 1.0
        request_metrics.record("POST", OPERATIONS, float(wall), 0.0, 0.0)
        request_metrics.persist()
    assert len(path.read_text().splitlines()) <= 3 * request_metrics.SAMPLES.maxlen
    reload(monkeypatch, now, path, 1.0)
    assert len(request_metrics.SAMPLES) == 64
    assert request_metrics.SAMPLES[-1][3] == 199.0


def test_corrupt_journal_lines_are_skipped(tmp_path, monkeypatch, now):
    monkeypatch.setattr(request_metrics, "STARTED_AT", START)
    path = tmp_path / "metrics.jsonl"
    corrupt = [
        "not json",
        "{}",
        "[]",
        '[["start"], 1.0]',
        '["begin", 1.0]',
        f'["start", {START - 20.0}, 1.0]',
        f'["s", {START - 4.0}, "POST", 7, 1.0, 0.0, 0.0]',
        f'["s", {START - 4.0}, "POST", "{OPERATIONS}", "slow", 0.0, 0.0]',
        '["flush", NaN]',
        '["flush", true]',
        '["flush", "later"]',
        f'["stop", {START - 3.0}, 1.0]',
    ]
    path.write_text(
        "\n".join(
            [
                f'["start", {START - 10.0}]',
                *corrupt,
                f'["s", {START - 5.0}, "POST", "{OPERATIONS}", 1.0, 0.0, 0.0]',
            ]
        )
        + "\n"
    )
    request_metrics.restore(path)
    assert [row[3] for row in request_metrics.SAMPLES] == [1.0]
    coverage = request_metrics.report(1.0)["coverage"]
    assert coverage["lost"] == [{"start": START - 5.0, "end": START}]
    assert coverage["held_since"] == START - 10.0


def test_reload_persists_a_stop_before_exec(tmp_path, now):
    path = tmp_path / "metrics.jsonl"
    request_metrics.restore(path)
    request_metrics.record("POST", OPERATIONS, 1.0, 0.0, 0.0)
    calls = []
    assert server.reload_if_changed(-1, (tmp_path,), lambda *args: calls.append(path.read_text()))
    assert json.loads(calls[0].splitlines()[-1])[0] == "stop"


def test_endpoint_and_command_return_route_records(tmp_path, now, metrics_url, capsys):
    request_metrics.record("POST", OPERATIONS, 1.78, 0.5, 0.75)
    with urlopen(f"{metrics_url}/api/v1/metrics?view=writes&hours=1&end={START}&limit=10") as response:
        body = json.load(response)
    assert body["routes"][0]["records"][0]["wall_seconds"] == 1.78
    assert body["window_end"] == START
    request_metrics.main(["--writes", "--hours", "1", "--end", str(START), "--limit", "10", "--url", metrics_url])
    assert json.loads(capsys.readouterr().out) == body


@pytest.mark.parametrize("query", ["view=writes&limit=0", "view=writes&limit=x", "view=nope", "end=nan"])
def test_endpoint_refuses_bad_reader_queries(now, metrics_url, query):
    with pytest.raises(HTTPError) as refused:
        urlopen(f"{metrics_url}/api/v1/metrics?hours=1&{query}")
    assert refused.value.code == 400


@pytest.mark.parametrize(
    ("args", "query"),
    [
        (["--writes"], "hours=24.0&view=writes&limit=1000"),
        (["--end", "5"], "hours=24.0&end=5.0"),
        (["--writes", "--limit", "10000", "--hours", "1"], "hours=1.0&view=writes&limit=10000"),
    ],
)
def test_command_builds_the_reader_query(monkeypatch, capsys, args, query):
    calls = []

    def read(url, timeout):
        calls.append((url, timeout))
        return io.StringIO("{}")

    monkeypatch.setattr(request_metrics, "urlopen", read)
    request_metrics.main([*args, "--url", "http://example:1"])
    assert calls == [(f"http://example:1/api/v1/metrics?{query}", 10)]


@pytest.mark.parametrize(
    ("args", "message"),
    [
        (["--end", "nan"], "end must be a finite epoch time"),
        (["--limit", "0"], "limit must be between 1 and 10000"),
        (["--limit", "10001"], "limit must be between 1 and 10000"),
    ],
)
def test_command_refuses_bad_reader_arguments(monkeypatch, capsys, args, message):
    monkeypatch.setattr(request_metrics, "urlopen", lambda *args, **kwargs: pytest.fail("no request"))
    with pytest.raises(SystemExit):
        request_metrics.main(args)
    assert capsys.readouterr().err.splitlines()[-1].endswith(f"error: {message}")


def test_server_keeps_serving_when_the_journal_cannot_be_read(tmp_path, now, capsys):
    server.metrics_journal(request_metrics.restore, tmp_path)
    assert request_metrics.JOURNAL is None
    assert capsys.readouterr().err.startswith("request metrics journal: ")
    coverage = request_metrics.report(2.0)["coverage"]
    assert coverage["held_since"] == request_metrics.STARTED_AT
    assert coverage["complete"] is False


def test_failed_journal_write_keeps_the_rows_for_the_next_pass(tmp_path, monkeypatch, now, capsys):
    path = tmp_path / "metrics.jsonl"
    request_metrics.restore(path)
    request_metrics.record("POST", OPERATIONS, 1.0, 0.0, 0.0)
    real = request_metrics.write_lines

    def full(*args):
        raise OSError("disk full")

    monkeypatch.setattr(request_metrics, "write_lines", full)
    server.metrics_journal(request_metrics.persist)
    assert capsys.readouterr().err == "request metrics journal: disk full\n"
    monkeypatch.setattr(request_metrics, "write_lines", real)
    now[0] += 2.0
    request_metrics.persist(stopped=True)
    reload(monkeypatch, now, path, 1.0)
    result = request_metrics.records(1.0, None, limit=10)
    assert result["coverage"]["lost"] == []
    assert [row["wall_seconds"] for row in result["routes"][0]["records"]] == [1.0]


def test_restore_binds_the_journal_only_after_its_rewrite(tmp_path, monkeypatch, now):
    path = tmp_path / "metrics.jsonl"
    path.write_text(f'["start", {START - 7000.0}]\n["s", {START - 10.0}, "POST", "{OPERATIONS}", 1.0, 0.0, 0.0]\n')

    def refused(*args):
        raise OSError("read only")

    monkeypatch.setattr(request_metrics, "write_lines", refused)
    with pytest.raises(OSError):
        request_metrics.restore(path)
    assert request_metrics.JOURNAL is None
    assert list(request_metrics.SAMPLES) == []
    coverage = request_metrics.report(2.0)["coverage"]
    assert coverage["held_since"] == request_metrics.STARTED_AT
    assert coverage["lost"] == []
    request_metrics.record("POST", OPERATIONS, 1.0, 0.0, 0.0)
    assert request_metrics.PENDING == []
