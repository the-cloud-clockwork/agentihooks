import errno
import json
import queue
import threading
from concurrent.futures import ThreadPoolExecutor
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer

import pytest

from scripts.swarm_ledger import ledger_server as server
from scripts.swarm_ledger import new_ledger
from tests.swarm_ledger import legacy_page

SLUG = "writer-burst"


@pytest.fixture
def listening(monkeypatch, ledger_port, tmp_path):
    admitted = threading.Event()
    admitted.set()
    created = queue.Queue()

    class ObservedServer(ThreadingHTTPServer):
        def server_activate(self):
            super().server_activate()
            created.put(self)

        def get_request(self):
            assert admitted.wait(15), "accept gate was never released"
            return super().get_request()

    monkeypatch.setattr(server, "ThreadingHTTPServer", ObservedServer)
    monkeypatch.setattr(server, "HOST", "127.0.0.1")
    monkeypatch.setattr(server, "PORT", ledger_port)
    monkeypatch.setattr(server, "PIDFILE", tmp_path / ".server.pid")
    monkeypatch.setattr(server, "ALLOWED_HOSTS", {f"127.0.0.1:{ledger_port}"})
    monkeypatch.setattr(server, "watch_ledgers", lambda: None)
    monkeypatch.setattr(server.server_lifetime, "watch", lambda *args: threading.Event())
    monkeypatch.setattr(server.server_code, "record", lambda *args: None)
    document = new_ledger.build_doc({"title": "Burst", "phases": [{"title": "Proof"} for _ in range(16)]})
    page = legacy_page.render(document, SLUG, ledger_port)
    html, _ = legacy_page.paths(SLUG, server.core)
    html.write_text(page)
    server.core.sync(SLUG, ops=[])
    token = server.core.read_token(page)
    worker = threading.Thread(target=server.serve)
    worker.start()
    httpd = created.get(timeout=10)
    try:
        yield httpd, admitted, token
    finally:
        admitted.set()
        httpd.shutdown()
        worker.join(timeout=10)
        assert not worker.is_alive()


def request(port, token, method, resource, body=None):
    connection = HTTPConnection("127.0.0.1", port, timeout=10)
    try:
        connection.request(
            method,
            f"/api/v1/ledgers/{SLUG}/{resource}",
            body=None if body is None else json.dumps(body),
            headers={"X-Ledger-Token": token, "Content-Type": "application/json"},
        )
        response = connection.getresponse()
        result = json.loads(response.read())
        assert response.status == 200, result
        return response.status, result
    finally:
        connection.close()


def test_production_listen_backlog_is_above_five(listening):
    httpd, _, _ = listening
    assert httpd.request_queue_size > 5


def burst_request(port, token, index, guard, start, connected):
    connection = HTTPConnection("127.0.0.1", port, timeout=0.5)
    error = None
    try:
        start.wait(timeout=10)
        try:
            connection.connect()
            connection.sock.settimeout(10)
        except OSError as exc:
            error = exc
        connected.wait(timeout=10)
        if error is not None:
            raise error
        resource = "phases?limit=16"
        method, body = "GET", None
        if index < 16:
            resource, method = "operations", "POST"
            thread = f"phases/p{index + 1}/comments"
            body = json.dumps(
                {
                    "operation_id": f"burst-write-{index}",
                    "guards": {thread: guard},
                    "ops": [{"op": "add", "id": f"burst-comment-{index}", "thread": thread, "text": "Burst write"}],
                }
            )
        connection.request(
            method,
            f"/api/v1/ledgers/{SLUG}/{resource}",
            body=body,
            headers={"X-Ledger-Token": token, "Content-Type": "application/json"},
        )
        response = connection.getresponse()
        return {"index": index, "status": response.status, "body": json.loads(response.read())}
    except OSError as exc:
        return {"index": index, "error": type(exc).__name__, "reset": exc.errno == errno.ECONNRESET}
    finally:
        connection.close()


def test_sixteen_writers_during_mixed_reads_complete_without_resets(listening):
    httpd, admitted, token = listening
    port = httpd.server_port
    guards = [request(port, token, "GET", f"phases/p{index + 1}/comments")[1]["revision"] for index in range(16)]
    start = threading.Barrier(21)
    connected = threading.Barrier(21)
    admitted.clear()
    try:
        with ThreadPoolExecutor(max_workers=20) as pool:
            futures = [
                pool.submit(burst_request, port, token, index, guards[index] if index < 16 else None, start, connected)
                for index in range(20)
            ]
            start.wait(timeout=10)
            try:
                connected.wait(timeout=10)
            finally:
                admitted.set()
            results = [future.result(timeout=15) for future in futures]
    finally:
        admitted.set()
    errors = [result for result in results if "error" in result]
    resets = sum(result.get("reset", False) for result in results)
    assert resets == 0, {"resets": resets, "errors": errors}
    assert errors == [], {"resets": resets, "errors": errors}
    assert all(result["status"] == 200 for result in results), results
    for result in results[:16]:
        assert result["body"]["applied"] == [f"burst-comment-{result['index']}"]
        assert result["body"]["rejected"] == []
    for index in range(16):
        status, data = request(port, token, "GET", f"phases/p{index + 1}/comments")
        assert status == 200
        assert [row["id"] for row in data["data"]] == [f"burst-comment-{index}"]
