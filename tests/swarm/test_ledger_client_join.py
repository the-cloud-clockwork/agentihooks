from types import SimpleNamespace

from scripts.swarm import ledger_client


def test_join_seats_the_named_agent_with_its_role(monkeypatch):
    calls = []

    def call(slug, ops, service):
        calls.append((slug, ops, service))
        return {"rejected": []}

    monkeypatch.setattr(ledger_client, "_ledger", lambda: SimpleNamespace(call=call))
    ledger_client.LedgerClient(service=True).join("demo", "master@a1b2c3-0002", "orchestrator")
    [(slug, [op], service)] = calls
    assert (slug, service) == ("demo", True)
    assert {k: v for k, v in op.items() if k != "id"} == {
        "op": "join",
        "by": "master@a1b2c3-0002",
        "role": "orchestrator",
    }
    assert op["id"].startswith("join-")
