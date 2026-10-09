import json

import pytest

from scripts.swarm_ledger import ledger_core as core
from scripts.swarm_ledger.repository import sqlite
from scripts.swarm_ledger.repository.sqlite import SQLiteLedgerRepository


@pytest.fixture
def stored(tmp_path, monkeypatch):
    monkeypatch.setenv("LEDGER_DIR", str(tmp_path))
    monkeypatch.setattr(core, "LEDGER_DIR", tmp_path)
    monkeypatch.setattr("hooks._redis.get_redis", lambda: None)
    repo = SQLiteLedgerRepository(tmp_path / sqlite.DATABASE).bound(core)
    repo.create(
        "chat-proof",
        {
            "title": "Demo",
            "overview": "Conversation",
            "sources": [],
            "phases": [{"title": "One", "description": "Work"}],
        },
    )
    repo.apply_ops("chat-proof", ops=[{"op": "join", "id": "join", "by": "boss", "role": "orchestrator"}])
    state, rejected = repo.apply_ops(
        "chat-proof",
        ops=[
            {"op": "add", "thread": "chat", "id": "question", "text": "Where are we?"},
            {"op": "add", "thread": "chat", "id": "addressed", "by": "boss", "to": "operator", "text": "Work started."},
            {
                "op": "add",
                "thread": "chat",
                "id": "answer",
                "by": "boss",
                "reply_to": "question",
                "text": "Tests pass.",
            },
        ],
    )
    assert rejected == []
    return repo, state


@pytest.mark.parametrize("suffix", ["json", "html"])
@pytest.mark.parametrize("entrypoint", ["get_document", "read", "apply_ops", "sync"])
def test_legacy_file_write_cannot_add_chat_to_stored_ledger(stored, suffix, entrypoint):
    repo, before = stored
    injected = {
        **before,
        "chat": [*before["chat"], {"id": "injected", "by": "boss", "at": 1, "text": "Work finished."}],
    }
    source = repo.directory / f"chat-proof.{suffix}"
    text = (
        json.dumps(injected)
        if suffix == "json"
        else f'<script id="ledger-data" type="application/json">{core.seed_text(injected)}</script>'
    )
    source.write_text(text, encoding="utf-8")
    reopened = SQLiteLedgerRepository(repo.path).bound(core)
    if entrypoint == "read":
        after = reopened.read("chat-proof", "chat")
    elif entrypoint == "apply_ops":
        after, rejected = reopened.apply_ops("chat-proof")
        assert rejected == []
    elif entrypoint == "sync":
        after, rejected = core.sync("chat-proof")
        assert rejected == []
    else:
        after = reopened.get_document("chat-proof")
    assert after["chat"] == before["chat"]
    assert reopened.get_document("chat-proof")["chat"] == before["chat"]


@pytest.mark.parametrize(
    "address,accepted", [({}, False), ({"to": "operator"}, True), ({"reply_to": "question"}, True)]
)
def test_stored_chat_accepts_only_operator_conversation(stored, address, accepted):
    repo, before = stored
    op = {"op": "add", "thread": "chat", "id": "next", "by": "boss", "text": "Work continues.", **address}
    core.check_op(op)
    after, rejected = repo.apply_ops("chat-proof", ops=[op])
    assert rejected == ([] if accepted else ["next"])
    assert after["chat"][: len(before["chat"])] == before["chat"]
    assert [entry["id"] for entry in after["chat"]] == [
        "question",
        "addressed",
        "answer",
        *(["next"] if accepted else []),
    ]
