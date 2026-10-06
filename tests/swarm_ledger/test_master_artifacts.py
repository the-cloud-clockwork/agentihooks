import json
import sys
import threading
import urllib.request
from http.server import ThreadingHTTPServer

import ledger
import ledger_core as core
import ledger_server as server
import pytest

from tests.swarm_ledger.test_bin import make_ledger

MASTER = "master@abcdef-0001"
WORKER = "engineer@abcdef-0002"
CONTENT = b"# Requested audit\n\nDesign and architecture findings.\n"


@pytest.fixture
def publication(ledger_dir, tmp_path, monkeypatch):
    folder = tmp_path / "ledgers"
    folder.mkdir()
    monkeypatch.setattr(core, "LEDGER_DIR", folder)
    slug = "master-artifacts"
    html, _ = make_ledger(slug)
    token = core.read_token(html.read_text())
    core.sync(slug, ops=[{"op": "join", "id": "join", "by": MASTER, "role": "orchestrator"}])
    core.sync(slug, ops=[{"op": "add", "id": "requested", "thread": "chat", "text": "Publish the audit summary"}])
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
    port = httpd.server_address[1]
    monkeypatch.setattr(server, "ALLOWED_HOSTS", {f"127.0.0.1:{port}"})
    monkeypatch.setattr(ledger, "BASE", f"http://127.0.0.1:{port}")
    thread = threading.Thread(target=httpd.serve_forever, args=(0.01,), daemon=True)
    thread.start()
    path = tmp_path / "audit.md"
    path.write_bytes(CONTENT)
    monkeypatch.setenv("AGENTIHOOKS_SWARM", slug)
    monkeypatch.setenv("AGENTIHOOKS_AGENT_NAME", MASTER)
    monkeypatch.setenv("AGENTIHOOKS_SWARM_LANE", "master")
    monkeypatch.setenv("AGENTIHOOKS_SWARM_TASK", "master")
    monkeypatch.setenv("AGENTIHOOKS_PROFILE", "master")
    yield slug, path, token
    httpd.shutdown()
    httpd.server_close()
    thread.join()


@pytest.mark.parametrize("harness", ["claude", "codex"])
def test_joined_master_publishes_requested_markdown_without_claims(publication, monkeypatch, capsys, harness):
    slug, path, _ = publication
    monkeypatch.setenv("AGENTIHOOKS_TARGET", harness)
    monkeypatch.setattr(
        sys,
        "argv",
        ["ledger", "--slug", slug, "--as", MASTER, "artifact", str(path), "Audit summary", "--request", "requested"],
    )
    ledger.main()
    assert json.loads(capsys.readouterr().out) == {"published": True}
    state = ledger.call(slug)
    [row] = state["artifacts"]
    assert (row["by"], row["task"], row["request"]) == (MASTER, "", "requested")
    assert state["_meta"]["members"][MASTER]["claims"] == []
    with urllib.request.urlopen(f"{ledger.BASE}/artifacts/{slug}/{row['file']['id']}") as response:
        assert response.read() == CONTENT


@pytest.mark.parametrize("task", ["master", ""])
def test_page_server_accepts_requested_master_artifact(publication, task):
    slug, path, _ = publication
    file = ledger.upload_artifact(slug, MASTER, str(path))
    op = {
        "op": "artifact_add",
        "id": "published",
        "by": MASTER,
        "task": task,
        "title": "Audit summary",
        "file": file,
        "request": "requested",
    }
    state = ledger.call(slug, [op])
    assert state["rejected"] == []
    [row] = state["artifacts"]
    assert (row["by"], row["task"], row["request"]) == (MASTER, "", "requested")


@pytest.mark.parametrize("request_id", [None, "missing", "agent-request_id", "deleted-request_id"])
def test_master_requires_operator_request(publication, request_id):
    slug, path, _ = publication
    core.sync(
        slug,
        ops=[
            {
                "op": "add",
                "id": "agent-request_id",
                "thread": "chat",
                "by": MASTER,
                "text": "Publish the audit summary",
            },
            {"op": "add", "id": "deleted-request_id", "thread": "chat", "text": "Publish the audit summary"},
            {"op": "delete", "id": "deleted-request_id", "thread": "chat"},
        ],
    )
    file = ledger.upload_artifact(slug, MASTER, str(path))
    op = {"op": "artifact_add", "id": "refused", "by": MASTER, "task": "master", "title": "Audit summary", "file": file}
    if request_id is not None:
        op["request"] = request_id
    state = ledger.call(slug, [op])
    assert state["rejected"] == ["refused"]
    assert state["artifacts"] == []


@pytest.mark.parametrize("by,role", [(MASTER, "member"), (WORKER, "orchestrator"), (WORKER, "member")])
def test_master_marker_needs_joined_master_identity_and_role(publication, by, role):
    slug, path, _ = publication
    core.sync(slug, ops=[{"op": "join", "id": "role", "by": by, "role": role}])
    file = ledger.upload_artifact(slug, by, str(path))
    op = {
        "op": "artifact_add",
        "id": "refused",
        "by": by,
        "task": "master",
        "title": "Audit summary",
        "file": file,
        "request": "requested",
    }
    state = ledger.call(slug, [op])
    assert state["rejected"] == ["refused"]
    assert state["artifacts"] == []


def test_unjoined_master_cannot_publish(publication):
    slug, path, _ = publication
    file = ledger.upload_artifact(slug, MASTER, str(path))
    core.sync(slug, ops=[{"op": "leave", "id": "leave", "by": MASTER}])
    op = {
        "op": "artifact_add",
        "id": "refused",
        "by": MASTER,
        "task": "master",
        "title": "Audit summary",
        "file": file,
        "request": "requested",
    }
    state = ledger.call(slug, [op])
    assert state["rejected"] == ["refused"]
    assert state["artifacts"] == []


@pytest.mark.parametrize("harness", ["claude", "codex"])
def test_worker_cannot_impersonate_master_in_cli(publication, monkeypatch, harness):
    slug, path, _ = publication
    monkeypatch.setenv("AGENTIHOOKS_TARGET", harness)
    monkeypatch.setenv("AGENTIHOOKS_AGENT_NAME", WORKER)
    monkeypatch.setenv("AGENTIHOOKS_SWARM_LANE", "eng")
    monkeypatch.setattr(
        sys,
        "argv",
        ["ledger", "--slug", slug, "--as", MASTER, "artifact", str(path), "Audit summary", "--request", "requested"],
    )
    with pytest.raises(SystemExit, match="cannot act as"):
        ledger.main()
    assert ledger.call(slug)["artifacts"] == []


def test_claimed_worker_keeps_requested_artifact_behavior(publication, monkeypatch, capsys):
    slug, path, _ = publication
    core.sync(
        slug,
        ops=[
            {"op": "join", "id": "worker", "by": WORKER},
            {
                "op": "task_add",
                "id": "task",
                "by": MASTER,
                "task": "work",
                "title": "Write summary",
                "lane": "eng",
                "artifact": True,
            },
            {
                "op": "task_update",
                "id": "claim",
                "by": WORKER,
                "item": "tasks/work",
                "fields": {"state": "claimed", "claimed_by": WORKER},
            },
            {"op": "claim", "id": "own", "by": WORKER, "item": "tasks/work"},
        ],
    )
    monkeypatch.setenv("AGENTIHOOKS_AGENT_NAME", WORKER)
    monkeypatch.setenv("AGENTIHOOKS_SWARM_LANE", "eng")
    monkeypatch.setenv("AGENTIHOOKS_SWARM_TASK", "work")
    monkeypatch.setattr(sys, "argv", ["ledger", "--slug", slug, "--as", WORKER, "artifact", str(path), "Audit summary"])
    ledger.main()
    assert json.loads(capsys.readouterr().out) == {"published": True}
    [row] = ledger.call(slug)["artifacts"]
    assert (row["by"], row["task"]) == (WORKER, "work")
