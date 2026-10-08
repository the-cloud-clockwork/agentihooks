from types import SimpleNamespace

from scripts.swarm import ledger_client


def test_relay_posts_the_senders_chat_line_with_the_service_credential(monkeypatch):
    calls = []

    def call(slug, ops, service):
        calls.append((slug, ops, service))
        return {"rejected": []}

    monkeypatch.setattr(ledger_client, "_ledger", lambda: SimpleNamespace(call=call))
    ledger_client.LedgerClient().relay("demo", "the docs are merged", "master@a1b2c3-0002")
    [(slug, [op], service)] = calls
    assert (slug, service) == ("demo", True)
    assert {k: v for k, v in op.items() if k != "id"} == {
        "op": "add",
        "by": "master@a1b2c3-0002",
        "thread": "chat",
        "text": "the docs are merged",
        "to": "operator",
    }


def test_notify_posts_a_swarm_notice_for_the_panel(monkeypatch):
    calls = []

    def call(slug, ops, service):
        calls.append((slug, ops))
        return {"rejected": []}

    monkeypatch.setattr(ledger_client, "_ledger", lambda: SimpleNamespace(call=call))
    ledger_client.LedgerClient().notify("demo", "The swarm is paused.")
    [(slug, [op])] = calls
    assert slug == "demo"
    assert {k: v for k, v in op.items() if k != "id"} == {"op": "notice", "by": "swarm", "text": "The swarm is paused."}
