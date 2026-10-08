import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from scripts.swarm_ledger import ledger_agent_ops, ledger_comments, new_ledger
from scripts.swarm_ledger import ledger_core as validation
from scripts.swarm_ledger import ledger_server as server
from tests.swarm_ledger import legacy_page  # noqa: E402

core = server.core


@pytest.fixture
def ledger_page(monkeypatch):
    doc = new_ledger.build_doc({"title": "Doctor proof", "phases": [{"title": "Fix"}]})
    doc["tasks"] = [{"id": "fx-8be892c4-code", "title": "Inbox fix", "phase": "p1", "comments": []}]
    html_path, _ = core.paths("demo")
    html_path.parent.mkdir(parents=True, exist_ok=True)
    html_path.write_text(legacy_page.render(doc, "demo", server.PORT))
    core.sync("demo", ops=[{"op": "join", "id": "join", "by": "engineer"}])
    token = legacy_page.stored_token(html_path)
    monkeypatch.setattr(server, "relay_to_inbox", lambda *args: None)
    monkeypatch.setattr(server, "doctor_phrase", lambda *args: None)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
    thread = threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.01})
    thread.start()

    def put(op=None):
        request = urllib.request.Request(
            f"http://127.0.0.1:{httpd.server_port}/api/demo?agent=engineer",
            json.dumps({"ops": [op]}).encode() if op is not None else b"",
            {"Host": f"127.0.0.1:{server.PORT}", "X-Ledger-Token": token, "Content-Type": "application/json"},
            method="PUT",
        )
        try:
            with urllib.request.urlopen(request, timeout=2) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read().decode()

    yield put
    httpd.shutdown()
    httpd.server_close()
    thread.join()


@pytest.mark.parametrize("kind", ["comment", "followup"])
def test_registered_doctor_task_passes_server_validation(ledger_page, kind):
    text = "Cut from the plan of task fx-8be892c4-code: more work"
    op = {"op": "add", "id": "entry", "by": "engineer", "thread": "tasks/fx-8be892c4-code/comments", "text": text}
    if kind == "followup":
        op.update(op="add_item", list="followups")
        del op["thread"]
    status, state = ledger_page(op)
    assert status == 200, state
    assert state["rejected"] == []
    doc = server.repository.get_document("demo")
    entries = doc["tasks"][0]["comments"] if kind == "comment" else doc["followups"]
    assert entries[-1]["text"] == text


@pytest.mark.parametrize(
    "text",
    [
        "Merged 8be892c4",
        "Task fx-1234abcd-code is ready",
        "Task fx-8be892c4-code-extra is ready",
        "Task fx-8be892c4-code and commit 8be892c4 are ready",
        "Task fx-8be892c4-code and commit a637e5ff are ready",
    ],
)
def test_server_still_refuses_real_hashes_and_unregistered_tokens(ledger_page, text):
    status, reason = ledger_page(
        {"op": "add", "id": "entry", "by": "engineer", "thread": "tasks/fx-8be892c4-code/comments", "text": text}
    )
    assert status == 400
    assert "commit hash" in reason


def test_audit_accepts_registered_tasks_and_keeps_real_hash_findings():
    text = "Cut from the plan of task fx-8be892c4-code: more work"
    doc = {
        "tasks": [{"id": "fx-8be892c4-code"}],
        "followups": [{"id": "f1", "text": text}],
        "phases": [{"id": "p1", "comments": [{"id": "c1", "by": "engineer", "text": text}]}],
        "chat": [{"id": "chat", "by": "engineer", "text": text}],
    }
    assert ledger_comments.audit(doc) == []
    doc["chat"][0]["text"] += " and commit 8be892c4"
    assert ledger_comments.audit(doc) == [("chat", "chat", "engineer", ["commit hash '8be892c4'"])]


@pytest.mark.parametrize("kind", ["comment", "followup", "status"])
def test_public_operation_validation_accepts_registered_task_and_refuses_hash(kind):
    task_ids = ("fx-8be892c4-code",)
    op = {"op": "add", "id": "entry", "by": "engineer", "thread": "phases/p1/comments", "text": "Task fx-8be892c4-code"}
    if kind == "followup":
        op.update(op="add_item", list="followups")
        del op["thread"]
    if kind == "status":
        op = {
            "op": "set",
            "id": "entry",
            "by": "engineer",
            "path": "phases/p1/done",
            "value": True,
            "status": op["text"],
        }
    if kind == "comment":
        validation.check_body({"ops": [op]}, task_ids)
    else:
        ledger_agent_ops.check(op, task_ids)
    op["status" if kind == "status" else "text"] += " and commit 8be892c4"
    with pytest.raises(ValueError, match="commit hash"):
        if kind == "comment":
            validation.check_body({"ops": [op]}, task_ids)
        else:
            ledger_agent_ops.check(op, task_ids)


def test_registered_task_context_preserves_long_chat_limit():
    op = {"op": "add", "id": "chat", "by": "engineer", "thread": "chat", "text": "word " * 101, "long": True}
    validation.check_body({"ops": [op]}, ("fx-8be892c4-code",))
    op["long"] = False
    with pytest.raises(ValueError, match="101 words"):
        validation.check_body({"ops": [op]}, ("fx-8be892c4-code",))


def test_empty_ledger_write_retains_registered_tasks(ledger_page):
    status, state = ledger_page()
    assert status == 200, state
    assert server.repository.get_document("demo")["tasks"][0]["id"] == "fx-8be892c4-code"


def test_plain_words_validation_keeps_the_default_chat_limit():
    assert ledger_comments.problems("word " * 101, "chat") == ["101 words, at most 100"]
    with pytest.raises(ValueError, match="101 words"):
        ledger_comments.check("word " * 101, "chat")


def test_audit_reports_all_real_hashes_and_ignores_operator_and_deleted_entries():
    text = "Task fx-8be892c4-code and commit a637e5ff"
    doc = {
        "tasks": [{"id": "fx-8be892c4-code"}],
        "phases": [{"id": "p1", "comments": [{"id": "c1", "by": "engineer", "text": text}]}],
        "followups": [{"id": "f1", "text": text}],
        "chat": [
            {"id": "chat", "by": "engineer", "text": text},
            {"id": "operator", "by": "operator", "text": text},
            {"id": "deleted", "by": "engineer", "text": text, "deleted": True},
        ],
    }
    assert ledger_comments.audit(doc) == [
        ("phases/p1", "c1", "engineer", ["commit hash 'a637e5ff'"]),
        ("followups/f1", "text", "", ["commit hash 'a637e5ff'"]),
        ("chat", "chat", "engineer", ["commit hash 'a637e5ff'"]),
    ]


def test_audit_without_tasks_still_reports_real_hashes():
    doc = {"chat": [{"id": "chat", "by": "engineer", "text": "Merged a637e5ff"}]}
    assert ledger_comments.audit(doc) == [("chat", "chat", "engineer", ["commit hash 'a637e5ff'"])]


def test_a_write_reconciles_the_ledger_once(ledger_page, monkeypatch):
    from scripts.swarm_ledger.repository import repository

    calls = []
    apply_ops = repository.apply_ops
    monkeypatch.setattr(
        repository, "apply_ops", lambda *args, **kwargs: calls.append(args) or apply_ops(*args, **kwargs)
    )
    text = "Cut from the plan of task fx-8be892c4-code: more work"
    op = {"op": "add", "id": "once", "by": "engineer", "thread": "tasks/fx-8be892c4-code/comments", "text": text}
    status, state = ledger_page(op)
    assert status == 200, state
    assert state["rejected"] == []
    assert len(calls) == 1
