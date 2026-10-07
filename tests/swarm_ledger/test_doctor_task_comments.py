import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

from scripts.swarm_ledger import ledger_server as server
from scripts.swarm_ledger import new_ledger

core = server.core


@pytest.fixture
def ledger_page(monkeypatch):
    doc = new_ledger.build_doc({"title": "Doctor proof", "phases": [{"title": "Fix"}]})
    doc["tasks"] = [{"id": "fx-8be892c4-code", "title": "Inbox fix", "phase": "p1", "comments": []}]
    html_path, _ = core.paths("demo")
    html_path.parent.mkdir(parents=True, exist_ok=True)
    html_path.write_text(new_ledger.render(doc, "demo", server.PORT))
    core.sync("demo", ops=[{"op": "join", "id": "join", "by": "engineer"}])
    token = core.read_token(html_path.read_text())
    monkeypatch.setattr(server, "relay_to_inbox", lambda *args: None)
    monkeypatch.setattr(server, "doctor_phrase", lambda *args: None)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
    thread = threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.01})
    thread.start()

    def put(op):
        request = urllib.request.Request(
            f"http://127.0.0.1:{httpd.server_port}/api/demo?agent=engineer",
            json.dumps({"ops": [op]}).encode(),
            {"Host": f"127.0.0.1:{server.PORT}", "X-Ledger-Token": token, "Content-Type": "application/json"},
            method="PUT",
        )
        try:
            with urllib.request.urlopen(request) as response:
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
    entries = state["tasks"][0]["comments"] if kind == "comment" else state["followups"]
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
    assert core.ledger_comments.audit(doc) == []
    doc["chat"][0]["text"] += " and commit 8be892c4"
    assert core.ledger_comments.audit(doc) == [("chat", "chat", "engineer", ["commit hash '8be892c4'"])]
