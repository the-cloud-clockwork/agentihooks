from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts.inbox.store import InboxStore
from scripts.swarm import delivery, ledger_client


def test_reply_and_page_relay_retries_keep_one_answer_and_one_page_post(tmp_path, monkeypatch):
    import fakeredis

    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[2] / "scripts" / "swarm_ledger"))
    from scripts.swarm_ledger import ledger_core
    from scripts.swarm_ledger.repository.legacy import fresh
    from scripts.swarm_ledger.repository.sqlite import SQLiteLedgerRepository

    repository = SQLiteLedgerRepository(tmp_path / "page.db", domain=ledger_core)
    doc, meta, _ = fresh(
        {"title": "Reply proof", "overview": "Inbox replies", "phases": [], "questions": [], "followups": []},
        core=ledger_core,
    )
    assert repository.create_document("reply-proof", doc, meta, token="fixture")

    def call(slug, ops=None, service=False):
        assert service
        state, rejected = repository.apply_ops(slug, ops=ops)
        return {**state, "rejected": rejected}

    monkeypatch.setattr(ledger_client, "_ledger", lambda: SimpleNamespace(call=call))
    box = InboxStore(fakeredis.FakeRedis(decode_responses=True))
    original = box.send("operator", "master", "status please")
    answer = box.reply(original.id, "master", "the work is complete")
    client = ledger_client.LedgerClient()
    agents = [SimpleNamespace(name="master")]
    close = box.close

    def lost_close(item_id, *args):
        if item_id == answer.id:
            raise ConnectionError("lost close acknowledgement")
        return close(item_id, *args)

    monkeypatch.setattr(box, "close", lost_close)
    with pytest.raises(ConnectionError, match="lost close acknowledgement"):
        delivery.relay_to_page(box, "reply-proof", agents, client)
    monkeypatch.setattr(box, "close", close)
    assert box.reply(original.id, "master", "the work is complete").id == answer.id
    assert delivery.relay_to_page(box, "reply-proof", agents, client) == 1
    state, rejected = repository.apply_ops("reply-proof")
    assert rejected == []
    assert [(post["id"], post["by"], post["text"]) for post in state["chat"]] == [
        (answer.id, "master", "the work is complete")
    ]
    assert [item.id for item in box.inbox("operator")] == [answer.id]
    assert box.get(answer.id).state == "done"
    assert delivery.relay_to_page(box, "reply-proof", agents, client) == 0
