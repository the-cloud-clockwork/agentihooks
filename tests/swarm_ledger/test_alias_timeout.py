import json
import threading
import urllib.request
from http.server import ThreadingHTTPServer
from unittest.mock import Mock

import pytest
from redis.exceptions import TimeoutError

from scripts.swarm_ledger import ledger_server as server
from scripts.swarm_ledger import new_ledger
from tests.swarm_ledger import legacy_page  # noqa: E402


@pytest.fixture
def ledger_page(monkeypatch, tmp_path):
    core = server.core
    monkeypatch.setattr(core, "LEDGER_DIR", tmp_path)
    doc = new_ledger.build_doc({"title": "Alias timeout proof", "phases": [{"title": "Keep requests readable"}]})
    html_path, _ = core.paths("demo")
    html_path.write_text(legacy_page.render(doc, "demo", server.PORT))
    core.sync("demo", ops=[{"op": "join", "id": "join", "by": "engineer"}])
    token = legacy_page.stored_token(html_path)
    redis = Mock()
    redis.get.side_effect = TimeoutError("Timeout reading from socket")
    redis.mget.side_effect = TimeoutError("Timeout reading from socket")
    monkeypatch.setattr("hooks._redis.get_redis", lambda: redis)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
    thread = threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.01})
    thread.start()
    try:
        yield httpd.server_port, token
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join()


def test_page_and_api_answer_when_alias_lookup_times_out(ledger_page, caplog):
    port, token = ledger_page
    for path, method in (("/demo", "GET"), ("/api/demo?view=agent", "PUT")):
        request = urllib.request.Request(
            f"http://127.0.0.1:{port}{path}",
            data=b"{}" if method == "PUT" else None,
            headers={"Host": f"127.0.0.1:{server.PORT}", "X-Ledger-Token": token, "Content-Type": "application/json"},
            method=method,
        )
        with urllib.request.urlopen(request, timeout=2) as response:
            assert response.status == 200
            body = response.read().decode()
            if path == "/demo":
                assert "Alias timeout proof" in body
            else:
                assert json.loads(body)["rejected"] == []
    view = server.ledger_view(server.repository.get_document("demo"))
    assert view["title"] == "Alias timeout proof"
    assert view["_meta"]["crew"][0]["name"] == "engineer"
    assert "alias lookup failed for 1 names" in caplog.text
    assert "Timeout reading from socket" in caplog.text
