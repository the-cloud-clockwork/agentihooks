import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts" / "swarm_ledger"))

from scripts.swarm_ledger import ledger_core as core  # noqa: E402
from scripts.swarm_ledger import ledger_op_checks as checks  # noqa: E402

MOVED = (
    "AGENT_OPS",
    "ARTIFACT_OPS",
    "SYNC_KINDS",
    "EXTENSION_OPS",
    "AUTHOR_RE",
    "MAX_TEXT",
    "kind_of",
    "check_op",
    "check_body",
)


@pytest.mark.parametrize("name", MOVED)
def test_core_serves_the_op_check_names(name):
    assert getattr(core, name) is getattr(checks, name)


def test_the_op_registry_names_each_op_family():
    assert checks.AGENT_OPS == (
        "join",
        "leave",
        "ack",
        "claim",
        "set",
        "add_item",
        "retext",
        "gate_bypass",
        "gate_lift",
    )
    assert checks.ARTIFACT_OPS == ("artifact_add", "artifact_delete", "artifact_restore", "artifact_purge")
    assert checks.SYNC_KINDS == {"sync": "sync requested", "stats_sync": "stats sync requested"}
    assert all(name in module.OPS for name, module in checks.EXTENSION_OPS.items())
    assert checks.MAX_TEXT == 20000


def test_kind_of_names_chat_and_comment_threads():
    assert (checks.kind_of("chat"), checks.kind_of("phases/p1/comments")) == ("chat", "comment")


def refusal(op, task_ids=()):
    with pytest.raises(ValueError) as caught:
        checks.check_op(op, task_ids)
    return str(caught.value)


def test_check_op_refuses_an_unknown_op_and_a_missing_id():
    assert refusal({"op": "nope", "id": "a"}) == "each op needs op add, edit, delete, clear, sync or an agent op"
    assert refusal({"op": "add", "id": ""}) == "each op needs a string id"
    assert refusal({"op": "add", "id": "a"}) == "each op needs a string thread"


def test_check_op_takes_a_sync_with_only_an_id():
    assert checks.check_op({"op": "sync", "id": "s"}) is None
    assert refusal({"op": "stats_sync", "id": "s", "by": "x"}) == "sync is the operator's and takes only an id"


def test_check_op_caps_text_and_keeps_long_to_agent_chat():
    long_text = "x" * (checks.MAX_TEXT + 1)
    assert refusal({"op": "add", "id": "a", "thread": "notes", "text": long_text}) == (
        f"add and edit need text up to {checks.MAX_TEXT} characters"
    )
    checks.check_op({"op": "add", "id": "a", "thread": "notes", "text": "x" * checks.MAX_TEXT})
    assert refusal({"op": "add", "id": "a", "thread": "notes", "text": "x", "long": True}) == (
        "long is allowed only on an agent chat message"
    )


def test_check_op_keeps_addresses_to_agent_chat():
    assert refusal({"op": "add", "id": "a", "thread": "notes", "text": "x", "to": "operator"}) == (
        "to addresses only an agent chat message or comment, reply_to only an agent chat message"
    )
    assert refusal({"op": "add", "id": "a", "thread": "chat", "text": "x", "by": "eng", "to": "eng"}) == (
        "an agent chat message goes to the operator or answers his line by its id"
    )


def test_check_op_keeps_by_to_agent_talk():
    assert refusal({"op": "add", "id": "a", "thread": "notes", "text": "x", "by": "eng"}) == (
        "by is allowed only on agent chat and comment entries, as an agent name"
    )
    assert refusal({"op": "add", "id": "a", "thread": "chat", "text": "x", "by": "operator"}) == (
        "by is allowed only on agent chat and comment entries, as an agent name"
    )


def test_check_op_keeps_attachments_to_talk_adds():
    assert refusal({"op": "edit", "id": "a", "thread": "chat", "text": "x", "attachments": []}) == (
        "attachments ride only on an add to chat or a comment thread"
    )


def test_check_body_returns_changes_and_ops():
    body = {"changes": [{"path": "phases/p1/done"}], "ops": [{"op": "clear", "id": "c", "thread": "chat"}]}
    assert checks.check_body(body) == (body["changes"], body["ops"])
    assert checks.check_body({}) == ([], [])


@pytest.mark.parametrize(
    ("body", "message"),
    (
        ([], "body must be an object"),
        ({"changes": {}}, "changes and ops must be lists"),
        ({"ops": {}}, "changes and ops must be lists"),
        ({"changes": [{"path": 1}]}, "each change needs a string path"),
    ),
)
def test_check_body_refuses_a_malformed_body(body, message):
    with pytest.raises(ValueError) as caught:
        checks.check_body(body)
    assert str(caught.value) == message
