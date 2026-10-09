import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts" / "swarm_ledger"
sys.path.insert(0, str(SCRIPTS))
import ledger_core as core  # noqa: E402
import new_ledger  # noqa: E402

from scripts.swarm_ledger.api import schemas  # noqa: E402
from scripts.swarm_ledger.api.errors import APIError  # noqa: E402

SLUG = "opchat-2026-01-01"


@pytest.fixture(autouse=True)
def ledger():
    content = {"title": "Demo", "overview": "o", "sources": [], "phases": [{"title": "one", "description": "d"}]}
    html_path, json_path = core.paths(SLUG)
    core.LEDGER_DIR.mkdir(parents=True, exist_ok=True)
    html_path.write_text(new_ledger.render(new_ledger.build_doc(content), SLUG, 8765), encoding="utf-8")
    json_path.unlink(missing_ok=True)
    core.sync(SLUG)
    core.sync(SLUG, ops=[{"op": "join", "id": "j1", "by": "boss", "role": "orchestrator"}])


def said(n, text, **fields):
    return {"op": "add", "thread": "chat", "id": f"m-{n}", "text": text, **fields}


def mutation(op):
    return {"operation_id": "w1", "ops": [op], "guards": {}}


def chat():
    state, _ = core.sync(SLUG)
    return [(e["by"], e["text"]) for e in state["chat"]]


def test_an_agent_line_addressed_to_nobody_is_refused():
    state, rejected = core.sync(SLUG, ops=[said(1, "The docs task is merged.", by="boss")])
    assert chat() == []
    assert rejected == ["m-1"]
    assert "chat is the operator's conversation" in " ".join(state["_meta"]["warnings"])


def test_an_agent_line_to_the_operator_is_shown():
    core.sync(SLUG, ops=[said(1, "Phase one is half done.", by="boss", to="operator")])
    assert chat() == [("boss", "Phase one is half done.")]


def test_an_agent_line_answering_an_operator_line_is_shown():
    core.sync(SLUG, ops=[said(1, "where are we")])
    core.sync(SLUG, ops=[said(2, "Phase one is half done.", by="boss", reply_to="m-1")])
    assert chat() == [("operator", "where are we"), ("boss", "Phase one is half done.")]


@pytest.mark.parametrize("reply_to", ["m-1", "m-9"])
def test_an_answer_to_an_agent_line_or_a_missing_line_is_refused(reply_to):
    core.sync(SLUG, ops=[said(1, "Phase one is half done.", by="boss", to="operator")])
    _, rejected = core.sync(SLUG, ops=[said(2, "Thanks.", by="boss", reply_to=reply_to)])
    assert rejected == ["m-2"]
    assert chat() == [("boss", "Phase one is half done.")]


def test_the_operator_writes_without_an_address():
    core.sync(SLUG, ops=[said(1, "where are we")])
    assert chat() == [("operator", "where are we")]


def test_a_notice_lands_in_the_notifications_panel_and_never_in_chat():
    state, _ = core.sync(SLUG, ops=[{"op": "notice", "id": "n1", "by": "swarm", "text": "The master is down."}])
    [row] = state["notifications"]
    assert (row["label"], row["text"], row["by"], row["item"]) == ("Swarm notice", "The master is down.", "swarm", "")
    assert chat() == []


@pytest.mark.parametrize(
    "op",
    [
        {"op": "notice", "id": "n1", "by": "swarm"},
        {"op": "notice", "id": "n1", "by": "swarm", "text": " "},
        {"op": "notice", "id": "n1", "text": "no author"},
        {"op": "notice", "id": "n1", "by": "operator", "text": "the operator posts in chat"},
        {"op": "notice", "id": "n1", "by": "swarm", "text": "x", "item": "chat"},
    ],
)
def test_a_malformed_notice_is_refused(op):
    with pytest.raises(ValueError):
        core.check_op(op)


@pytest.mark.parametrize(
    "op",
    [
        said(1, "hello", by="boss", to="operator"),
        said(1, "hello", by="boss", reply_to="m-0"),
        {"op": "notice", "id": "n1", "by": "swarm", "text": "The master is down."},
    ],
)
def test_the_api_schema_takes_an_address_and_a_notice(op):
    assert schemas.check_operations(mutation(op), core, ()) == [op]


def test_the_api_schema_refuses_an_address_to_an_agent():
    with pytest.raises(APIError, match="to"):
        schemas.check_operations(mutation(said(1, "hello", by="boss", to="ci")), core, ())
