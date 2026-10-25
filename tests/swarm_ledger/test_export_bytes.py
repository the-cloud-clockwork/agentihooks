import http.client

from scripts.swarm_ledger import ledger_server
from scripts.swarm_ledger.api import routes
from tests.swarm_ledger.test_api_v1 import authority_live, live
from tests.swarm_ledger.test_ledger_authority import SLUG, send

__all__ = ["authority_live", "live"]


def test_export_sends_the_cached_view_bytes_without_copying_them(live, monkeypatch):
    sent, send_export = [], ledger_server.Handler.send_export

    def capture(handler, data):
        sent.append(data)
        return send_export(handler, data)

    monkeypatch.setattr(ledger_server.Handler, "send_export", capture)
    headers = {"X-Ledger-Token": live["admin"], "Content-Type": "application/json"}
    replies = [send(live, "POST", f"/api/v1/ledgers/{SLUG}/export", b"{}", **headers) for _ in range(2)]
    assert [reply[0] for reply in replies] == [200, 200]
    assert replies[0][1] == replies[1][1] == sent[0]
    assert type(sent[0]) is bytes and sent[0] is sent[1]


def test_export_bytes_are_the_utf8_export_body():
    state = {"title": "café", "_meta": {"seeds": [1], "members": ["m"]}}
    assert routes.export_bytes(state) == routes.export_body(state).encode("utf-8")
    assert type(routes.export_bytes(state)) is bytes


def test_export_answers_a_file_page_with_its_cors_origin(live):
    headers = {"Host": live["host"], "X-Ledger-Token": live["admin"], "Content-Type": "application/json"}
    replies = {}
    for origin in (ledger_server.FILE_ORIGIN, None):
        conn = http.client.HTTPConnection("127.0.0.1", live["port"], timeout=5)
        try:
            extra = {} if origin is None else {"Origin": origin}
            conn.request("POST", f"/api/v1/ledgers/{SLUG}/export", b"{}", {**headers, **extra})
            response = conn.getresponse()
            response.read()
            replies[origin] = (response.status, response.getheader("Access-Control-Allow-Origin"))
        finally:
            conn.close()
    assert replies == {ledger_server.FILE_ORIGIN: (200, ledger_server.FILE_ORIGIN), None: (200, None)}
