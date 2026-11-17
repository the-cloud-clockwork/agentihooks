import errno
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
from typing import Any

import pytest

from scripts.swarm_ledger import api_service, new_ledger
from scripts.swarm_ledger import ledger_server as server
from tests.swarm_ledger import legacy_page

SLUG = "writer-burst"


@pytest.fixture
def listening(monkeypatch, ledger_port, tmp_path):
    admitted = threading.Event()
    admitted.set()

    class ObservedServer(ThreadingHTTPServer):
        def __init__(self, server_address: tuple[str, int], handler: type) -> None:
            super().__init__(server_address, handler, bind_and_activate=False)
            self.request_queue_size = 128
            self.server_bind()
            self.server_activate()

        def get_request(self) -> tuple[Any, Any]:
            assert admitted.wait(15), "accept gate was never released"
            return super().get_request()

    read_port = ledger_port
    write_httpd = ObservedServer(("127.0.0.1", 0), api_service.WriteHandler)
    write_port = write_httpd.server_address[1]
    monkeypatch.setattr(server, "ALLOWED_HOSTS", {f"127.0.0.1:{read_port}", f"127.0.0.1:{write_port}"})
    document = new_ledger.build_doc({"title": "Burst", "phases": [{"title": "Proof"} for _ in range(16)]})
    page = legacy_page.render(document, SLUG, read_port)
    html, _ = legacy_page.paths(SLUG, server.core)
    html.write_text(page)
    server.core.sync(SLUG, ops=[])
    token = server.core.read_token(page)
    read_httpd = ThreadingHTTPServer(("127.0.0.1", read_port), server.Handler)
    read_worker = threading.Thread(target=read_httpd.serve_forever, args=(0.01,), daemon=True)
    write_worker = threading.Thread(target=write_httpd.serve_forever, args=(0.01,), daemon=True)
    read_worker.start()
    write_worker.start()
    try:
        yield read_httpd, write_httpd, admitted, token
    finally:
        admitted.set()
        read_httpd.shutdown()
        write_httpd.shutdown()
        read_httpd.server_close()
        write_httpd.server_close()
        read_worker.join(timeout=10)
        write_worker.join(timeout=10)


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
    _, write_httpd, _, _ = listening
    assert write_httpd.request_queue_size > 5


def burst_request(read_port, write_port, token, index, guard, start, connected):
    port = write_port if index < 16 else read_port
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
    read_httpd, write_httpd, admitted, token = listening
    read_port, write_port = read_httpd.server_port, write_httpd.server_port
    guards = [request(read_port, token, "GET", f"phases/p{index + 1}/comments")[1]["revision"] for index in range(16)]
    start = threading.Barrier(21)
    connected = threading.Barrier(21)
    admitted.clear()
    try:
        with ThreadPoolExecutor(max_workers=20) as pool:
            futures = [
                pool.submit(
                    burst_request,
                    read_port,
                    write_port,
                    token,
                    index,
                    guards[index] if index < 16 else None,
                    start,
                    connected,
                )
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
        status, data = request(read_port, token, "GET", f"phases/p{index + 1}/comments")
        assert status == 200
        assert [row["id"] for row in data["data"]] == [f"burst-comment-{index}"]
