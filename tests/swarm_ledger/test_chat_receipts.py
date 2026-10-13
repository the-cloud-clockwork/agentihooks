from pathlib import Path

from scripts.swarm_ledger import ledger_server
from tests.swarm_ledger.test_operator_inbox import SLUG, make_ledger
from tests.swarm_ledger.test_operator_inbox import inbox as inbox_fixture

inbox = inbox_fixture


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
    assert receipt["to"] == "operator"
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
