from pathlib import Path

from scripts.swarm_ledger import ledger_server
from tests.swarm_ledger.test_operator_inbox import SLUG, make_ledger
from tests.swarm_ledger.test_operator_inbox import inbox as inbox_fixture

inbox = inbox_fixture


from scripts.swarm.store import AgentRecord, RedisStore
from scripts.swarm_ledger import chat_receipts


def test_master_rule_answers_each_operator_chat_with_first_tool_call():
    rule = Path(__file__).resolve().parents[2] / "profiles/package/roles/master/.claude/rules/master-role.md"
    text = rule.read_text()
    assert "Answer every operator chat message with your first tool call" in text
    assert "say what you will do" in text
    assert "follow with the result" in text


def test_server_receives_master_chat_immediately_without_consuming_the_inbox(inbox):
    make_ledger()
    state, _ = ledger_server.repository.apply_ops(
        SLUG, ops=[{"op": "add", "thread": "chat", "id": "operator-line", "text": "check the worker"}]
    )
    [item] = ledger_server.relay_to_inbox(SLUG, state)
    current = ledger_server.repository.get_document(SLUG)
    [original, receipt] = current["chat"]
    assert original["id"] == "operator-line"
    assert receipt["text"] == "Received by the master inbox."
    assert receipt["by"] == "swarm" and receipt["reply_to"] == original["id"]
    assert receipt.get("to", "operator") == "operator"
    assert inbox.get(item.id).state == "pending"
    assert ledger_server.relay_to_inbox(SLUG, state) == []
    assert ledger_server.repository.get_document(SLUG)["chat"] == current["chat"]


def test_failed_delivery_leaves_operator_chat_without_a_received_line(inbox, monkeypatch):
    from scripts.swarm import operator_mail

    make_ledger()
    state, _ = ledger_server.repository.apply_ops(
        SLUG, ops=[{"op": "add", "thread": "chat", "id": "operator-line", "text": "check the worker"}]
    )
    with monkeypatch.context() as patch:
        patch.setattr(operator_mail, "relay", lambda *args: (_ for _ in ()).throw(ConnectionError("offline")))
        assert ledger_server.relay_to_inbox(SLUG, state) == []
    assert len(ledger_server.repository.get_document(SLUG)["chat"]) == 1


def test_busy_secondary_master_receives_chat_without_reading_it(inbox):
    store = RedisStore(inbox.redis)
    store.put_agent(SLUG, AgentRecord("busy-master", "master", "", seat=f"master-2@{SLUG}"))
    make_ledger()
    state, _ = ledger_server.repository.apply_ops(
        SLUG, ops=[{"op": "add", "thread": "chat", "id": "busy-line", "text": "check the worker"}]
    )
    [item] = ledger_server.relay_to_inbox(SLUG, state)
    assert item.address == f"master-2@{SLUG}"
    assert inbox.get(item.id).state == "pending"
    assert ledger_server.repository.get_document(SLUG)["chat"][-1]["text"] == "Received by the master inbox."


def test_worker_chat_does_not_claim_a_master_received_it(inbox):
    RedisStore(inbox.redis).put_agent(SLUG, AgentRecord("worker", "eng", "", seat=f"eng-1@{SLUG}"))
    make_ledger()
    state, _ = ledger_server.repository.apply_ops(
        SLUG, ops=[{"op": "add", "thread": "chat", "id": "worker-line", "text": "@worker check the worker"}]
    )
    [item] = ledger_server.relay_to_inbox(SLUG, state)
    assert item.address == f"eng-1@{SLUG}"
    assert len(ledger_server.repository.get_document(SLUG)["chat"]) == 1


def test_comments_and_chat_edits_do_not_create_new_chat_receipts(inbox):
    phase = make_ledger()
    state, _ = ledger_server.repository.apply_ops(
        SLUG, ops=[{"op": "add", "thread": "chat", "id": "edit-line", "text": "check the worker"}]
    )
    ledger_server.relay_to_inbox(SLUG, state)
    state, _ = ledger_server.repository.apply_ops(
        SLUG, ops=[{"op": "edit", "thread": "chat", "id": "edit-line", "text": "check another worker"}]
    )
    ledger_server.relay_to_inbox(SLUG, state)
    state, _ = ledger_server.repository.apply_ops(
        SLUG,
        ops=[{"op": "add", "thread": f"phases/{phase}/comments", "id": "comment-line", "text": "check the worker"}],
    )
    ledger_server.relay_to_inbox(SLUG, state)
    assert len(ledger_server.repository.get_document(SLUG)["chat"]) == 2


def test_receipt_write_can_retry_without_delivering_chat_again(inbox, monkeypatch):
    make_ledger()
    state, _ = ledger_server.repository.apply_ops(
        SLUG, ops=[{"op": "add", "thread": "chat", "id": "retry-line", "text": "check the worker"}]
    )
    with monkeypatch.context() as patch:
        patch.setattr(
            ledger_server.repository, "apply_ops", lambda *args, **kwargs: (_ for _ in ()).throw(OSError("disk"))
        )
        assert ledger_server.relay_to_inbox(SLUG, state) == []
    assert len(ledger_server.repository.get_document(SLUG)["chat"]) == 1
    assert ledger_server.relay_to_inbox(SLUG, state) == []
    assert len(ledger_server.repository.get_document(SLUG)["chat"]) == 2
    assert len(inbox.pending_items(f"master@{SLUG}")) == 1


def test_receipt_posting_is_idempotent_for_the_same_accepted_message(inbox):
    make_ledger()
    state, _ = ledger_server.repository.apply_ops(
        SLUG, ops=[{"op": "add", "thread": "chat", "id": "repeat-line", "text": "check the worker"}]
    )
    [item] = ledger_server.relay_to_inbox(SLUG, state)
    chat_receipts.post(ledger_server.repository, inbox, SLUG, state["_meta"]["events"], [item.address])
    assert len(ledger_server.repository.get_document(SLUG)["chat"]) == 2


def test_chat_after_another_operator_write_in_the_same_batch_receives_its_receipt(inbox):
    phase = make_ledger()
    state, _ = ledger_server.repository.apply_ops(
        SLUG,
        ops=[
            {"op": "add", "thread": f"phases/{phase}/comments", "id": "before-chat", "text": "check the worker"},
            {"op": "add", "thread": "chat", "id": "after-comment", "text": "report the worker"},
        ],
    )
    assert len(ledger_server.relay_to_inbox(SLUG, state)) == 2
    [source, receipt] = ledger_server.repository.get_document(SLUG)["chat"]
    assert source["id"] == "after-comment"
    assert receipt["reply_to"] == source["id"]
    assert receipt["text"] == "Received by the master inbox."
