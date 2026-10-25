from scripts.swarm_ledger import ledger_server
from tests.swarm_ledger.test_api_v1 import authority_live, live
from tests.swarm_ledger.test_ledger_authority import SLUG, send

__all__ = ["authority_live", "live"]


def test_export_sends_the_cached_view_bytes_without_copying_them(live, monkeypatch):
    sent, send_reply = [], ledger_server.Handler.send

    def capture(handler, code, body, ctype):
        sent.append(body)
        return send_reply(handler, code, body, ctype)

    monkeypatch.setattr(ledger_server.Handler, "send", capture)
    headers = {"X-Ledger-Token": live["admin"], "Content-Type": "application/json"}
    replies = [send(live, "POST", f"/api/v1/ledgers/{SLUG}/export", b"{}", **headers) for _ in range(2)]
    assert [reply[0] for reply in replies] == [200, 200]
    assert replies[0][1] == replies[1][1] == sent[0]
    assert type(sent[0]) is bytes and sent[0] is sent[1]
