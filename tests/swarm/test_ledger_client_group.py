from types import SimpleNamespace

from scripts.swarm import ledger_client


def test_group_tasks_sends_the_lead_and_its_members_as_the_swarm(monkeypatch):
    sent = []

    def call(slug, batch, service):
        sent.extend((slug, op, service) for op in batch)
        return {"rejected": []}

    monkeypatch.setattr(ledger_client, "_ledger", lambda: SimpleNamespace(call=call))
    ledger_client.LedgerClient().group_tasks("demo", "t1", ("t2", "t3"))
    [(slug, op, service)] = sent
    assert (slug, service) == ("demo", True)
    assert op["id"].startswith("task_group-")
    assert {k: v for k, v in op.items() if k != "id"} == {
        "op": "task_group",
        "by": "swarm",
        "item": "tasks/t1",
        "members": ["t2", "t3"],
    }


def test_ungroup_tasks_sends_the_lead_as_the_swarm(monkeypatch):
    sent = []

    def call(slug, batch, service):
        sent.extend((slug, op, service) for op in batch)
        return {"rejected": []}

    monkeypatch.setattr(ledger_client, "_ledger", lambda: SimpleNamespace(call=call))
    ledger_client.LedgerClient().ungroup_tasks("demo", "t1")
    [(slug, op, service)] = sent
    assert (slug, service) == ("demo", True)
    assert op["id"].startswith("task_ungroup-")
    assert {k: v for k, v in op.items() if k != "id"} == {"op": "task_ungroup", "by": "swarm", "item": "tasks/t1"}
