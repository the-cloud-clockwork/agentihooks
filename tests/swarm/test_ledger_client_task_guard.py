from types import SimpleNamespace

import pytest

from scripts.swarm import ledger_client

ROWS = [{"id": "t0", "state": "open"}, {"id": "t1", "state": "done", "claimed_by": "engineer@a1b2c3-0001"}]


@pytest.fixture
def sent(monkeypatch):
    ops = []

    def call(slug, batch):
        ops.extend((slug, op) for op in batch)
        return {"rejected": [], "tasks": ROWS}

    monkeypatch.setattr(ledger_client, "_ledger", lambda: SimpleNamespace(call=call))
    return ops


def without_id(sent):
    return [(slug, {k: v for k, v in op.items() if k != "id"}) for slug, op in sent]


def test_a_guarded_update_sends_its_states_and_returns_the_live_row(sent):
    live = ledger_client.LedgerClient().update_task("demo", "t1", {"state": "open"}, if_state=("claimed", "pr"))
    assert live == ROWS[1]
    assert without_id(sent) == [
        (
            "demo",
            {
                "op": "task_update",
                "by": "swarm",
                "item": "tasks/t1",
                "fields": {"state": "open"},
                "if_state": ["claimed", "pr"],
            },
        )
    ]
    assert sent[0][1]["id"].startswith("task_update-")


def test_an_unguarded_update_carries_no_guard_and_names_its_author(sent):
    live = ledger_client.LedgerClient().update_task("demo", "t0", {"pr_url": "https://x/1"}, by="engineer@a1b2c3-0002")
    assert live == ROWS[0]
    assert without_id(sent) == [
        (
            "demo",
            {
                "op": "task_update",
                "by": "engineer@a1b2c3-0002",
                "item": "tasks/t0",
                "fields": {"pr_url": "https://x/1"},
            },
        )
    ]


def test_an_update_of_a_task_the_ledger_lacks_returns_an_empty_row(sent):
    assert ledger_client.LedgerClient().update_task("demo", "t9", {"state": "open"}) == {}
