import http.client
import json

from scripts.swarm_ledger import ledger_server
from scripts.swarm_ledger.api import routes
from scripts.swarm_ledger.repository.fragments import document
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


def test_export_text_hides_seeds_and_receipts_and_keeps_text_unescaped():
    state = {"title": "café", "_meta": {"seeds": [1], "api_operations": {"a": 1}, "members": ["m"]}}
    assert routes.export_text(document(state)) == '{"data":{"title":"café","_meta":{"members":["m"]}}}'.encode("utf-8")
    assert type(routes.export_text(document(state))) is bytes


def test_export_text_matches_the_hidden_view_of_any_ledger():
    state = {"title": "🟣", "_meta": {"seeds": [1], "api_operations": {}, "events": [{"n": 1.5}]}, "notes": ["x"] * 3}
    data = routes.export_text(document(state))
    assert (
        data
        == json.dumps(
            {"data": {**state, "_meta": {"events": [{"n": 1.5}]}}}, ensure_ascii=False, separators=(",", ":")
        ).encode()
    )


def test_a_later_export_reuses_the_text_of_every_section_no_write_changed():
    first = document({"title": "t", "tasks": [{"id": "a"}, {"id": "b"}], "_meta": {"rev": 1}})
    routes.export_text(first)
    second = document({"title": "t", "tasks": [{"id": "a"}, {"id": "c"}], "_meta": {"rev": 2}}, first)
    kept = second.parts["tasks"].parts[0]
    assert kept is first.parts["tasks"].parts[0] and kept.data == b'{"id":"a"}'
    assert second.parts["title"].data == b'"t"' and second.parts["tasks"].data is None
    assert json.loads(routes.export_text(second)) == {
        "data": {"title": "t", "tasks": [{"id": "a"}, {"id": "c"}], "_meta": {"rev": 2}}
    }
    assert second.parts["tasks"].data == b'[{"id":"a"},{"id":"c"}]'


def test_export_response_is_never_cached(live):
    conn = http.client.HTTPConnection("127.0.0.1", live["port"], timeout=5)
    try:
        headers = {"Host": live["host"], "X-Ledger-Token": live["admin"], "Content-Type": "application/json"}
        conn.request("POST", f"/api/v1/ledgers/{SLUG}/export", b"{}", headers)
        response = conn.getresponse()
        body = response.read()
        assert (response.status, response.getheader("Cache-Control")) == (200, "no-store")
        assert response.getheader("Content-Type") == "application/json"
        assert response.getheader("Content-Length") == str(len(body))
    finally:
        conn.close()
