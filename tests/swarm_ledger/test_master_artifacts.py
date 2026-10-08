import json
import sys
import threading
import urllib.request
from http.server import ThreadingHTTPServer

import ledger
import ledger_core as core
import pytest

from scripts.swarm_ledger import ledger_artifacts as artifacts
from scripts.swarm_ledger import ledger_server as server
from tests.swarm_ledger import legacy_page  # noqa: E402
from tests.swarm_ledger.test_bin import make_ledger

MASTER = "master@abcdef-0001"
WORKER = "engineer@abcdef-0002"
CONTENT = b"# Requested audit\n\nDesign and architecture findings.\n"


@pytest.fixture
def publication(ledger_dir, tmp_path, monkeypatch):
    monkeypatch.setitem(sys.modules, "ledger_artifacts", artifacts)
    folder = tmp_path / "ledgers"
    folder.mkdir()
    monkeypatch.setattr(core, "LEDGER_DIR", folder)
    slug = "master-artifacts"
    html, _ = make_ledger(slug)
    token = legacy_page.stored_token(html)
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
    [row] = ledger.resource(slug, "artifacts", collection=True)
    assert (row["by"], row["task"], row["request"]) == (MASTER, "", "requested")
    assert state["_meta"]["members"][MASTER]["claims"] == []
    with urllib.request.urlopen(f"{ledger.BASE}/artifacts/{slug}/{row['file']['id']}") as response:
        assert response.read() == CONTENT


@pytest.mark.parametrize("task", ["master", ""])
def test_page_server_accepts_requested_master_artifact(publication, task):
    slug, path, _ = publication
    file = ledger.upload_artifact(
        slug, MASTER, str(path), {"task": "", "title": "Audit summary", "request": "requested"}
    )
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
    [row] = ledger.resource(slug, "artifacts", collection=True)
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
    file = ledger.upload_artifact(
        slug, MASTER, str(path), {"task": "", "title": "Audit summary", "request": "requested"}
    )
    op = {"op": "artifact_add", "id": "refused", "by": MASTER, "task": "master", "title": "Audit summary", "file": file}
    if request_id is not None:
        op["request"] = request_id
    state = ledger.call(slug, [op])
    assert state["rejected"] == ["refused"]
    assert artifacts.REFUSED in state["_meta"]["warnings"]
    assert ledger.resource(slug, "artifacts", collection=True) == []


@pytest.mark.parametrize("by,role", [(MASTER, "member"), (WORKER, "orchestrator"), (WORKER, "member")])
def test_master_marker_needs_joined_master_identity_and_role(publication, monkeypatch, by, role):
    slug, path, _ = publication
    monkeypatch.setenv("AGENTIHOOKS_AGENT_NAME", by)
    core.sync(slug, ops=[{"op": "join", "id": "role", "by": by, "role": role}])
    file = ledger.upload_artifact(slug, by, str(path), {"task": "", "title": "Audit summary", "request": "requested"})
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
    assert ledger.resource(slug, "artifacts", collection=True) == []


def test_unjoined_master_cannot_publish(publication):
    slug, path, _ = publication
    file = ledger.upload_artifact(
        slug, MASTER, str(path), {"task": "", "title": "Audit summary", "request": "requested"}
    )
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
    assert ledger.resource(slug, "artifacts", collection=True) == []


@pytest.mark.parametrize("title", ["", "x" * 1000, "proof-file.md"])
def test_invalid_publication_title_leaves_the_media_folder_unchanged(publication, title):
    slug, path, _ = publication
    folder = artifacts.media.folder(slug)
    before = {p.name: p.read_bytes() for p in folder.glob("*") if p.is_file()}
    args = ledger.build_parser().parse_args(
        [
            "--slug",
            slug,
            "--as",
            MASTER,
            "artifact",
            str(path),
            title,
            "--request",
            "requested",
        ]
    )
    with pytest.raises(SystemExit):
        ledger.cmd_artifact(args)
    assert {p.name: p.read_bytes() for p in folder.glob("*") if p.is_file()} == before


def test_master_publication_on_a_real_task_keeps_its_task(publication):
    slug, path, _ = publication
    core.sync(
        slug,
        ops=[
            {
                "op": "task_add",
                "id": "add-work",
                "by": MASTER,
                "task": "work",
                "title": "Requested document",
                "lane": "eng",
                "artifact": True,
            }
        ],
    )
    file = ledger.upload_artifact(slug, MASTER, str(path), {"task": "work", "title": "Audit summary"})
    state = ledger.call(
        slug,
        [
            {
                "op": "artifact_add",
                "id": "published-work",
                "by": MASTER,
                "task": "work",
                "title": "Audit summary",
                "file": file,
            }
        ],
    )
    assert not state["rejected"]
    state["artifacts"] = ledger.resource(slug, "artifacts")
    assert state["artifacts"][-1]["task"] == "work"


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
