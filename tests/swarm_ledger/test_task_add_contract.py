import json
import sys
import threading
import uuid
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from scripts.swarm_ledger import ledger, ledger_server, new_ledger
from scripts.swarm_ledger.api import schemas
from tests.swarm_ledger import legacy_page


@pytest.fixture
def cli(monkeypatch, ledger_port, capsys):
    slug = f"task-add-{uuid.uuid4().hex}"
    server = ledger_server
    core = server.core
    core.LEDGER_DIR.mkdir(parents=True, exist_ok=True)
    document = new_ledger.build_doc({"title": "Task contract", "phases": [{"title": "Proof"}]})
    legacy_page.paths(slug, core)[0].write_text(legacy_page.render(document, slug, ledger_port))
    core.sync(slug, ops=[])
    host = f"127.0.0.1:{ledger_port}"
    monkeypatch.setattr(server, "ALLOWED_HOSTS", {host})
    monkeypatch.setattr(ledger, "BASE", f"http://{host}")
    monkeypatch.setenv("AGENTIHOOKS_DEPLOYMENT", "local")
    monkeypatch.delenv("AGENTIHOOKS_AGENT_NAME", raising=False)
    monkeypatch.delenv("AGENTIHOOKS_SWARM", raising=False)
    monkeypatch.setattr(server, "relay_to_inbox", lambda *args: None)
    monkeypatch.setattr(server, "doctor_phrase", lambda *args: None)

    def invoke(*options):
        monkeypatch.setattr(sys, "argv", ["ledger", "--slug", slug, "--as", "liaison", *options])
        code = 0
        try:
            ledger.main()
        except SystemExit as exc:
            code = exc.code
        output = capsys.readouterr()
        return code, output.out, output.err

    with ThreadingHTTPServer(("127.0.0.1", ledger_port), server.Handler) as httpd:
        thread = threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.01})
        thread.start()
        try:
            yield invoke
        finally:
            httpd.shutdown()
            thread.join(timeout=5)
            assert not thread.is_alive()


@pytest.mark.parametrize(
    ("option", "value", "field", "expected"),
    [
        ("--overlays", "tuner,trader", "overlays", ["tuner", "trader"]),
        ("--repositories", "/workspace/one,/workspace/two", "repositories", ["/workspace/one", "/workspace/two"]),
    ],
)
def test_task_add_client_fields_cross_the_real_server(cli, option, value, field, expected):
    code, output, error = cli("task", "add", "t1", "Build contract", option, value)
    assert code == 0, (output, error, code)
    assert json.loads(output) == {"task": "t1", "added": "Build contract"}
    code, output, error = cli("show")
    assert code == 0, error
    assert json.loads(output)["tasks"][0][field] == expected
    assert field in schemas.operation_schema("task_add")["properties"]


@pytest.mark.parametrize(
    ("option", "value", "reason"),
    [
        ("--overlays", "a,b,c,d", "overlays lists at most 3 overlay names"),
        ("--repositories", "relative/repo", "repositories must list absolute local repository paths"),
    ],
)
def test_invalid_client_fields_keep_their_domain_validation(cli, option, value, reason):
    code, output, error = cli("task", "add", "t1", "Invalid contract", option, value)
    assert code != 0
    assert reason in str(code) + error
    assert '"added"' not in output
    code, output, error = cli("show")
    assert code == 0, error
    assert json.loads(output)["tasks"] == []


@pytest.mark.parametrize("options", [("--repositories", "relative/repo"), ("--depends-on", "missing")])
def test_refused_scaffold_leaves_zero_new_folders(cli, options):
    root = Path.home() / ".agentihooks" / "swarm"
    before = set(root.glob("*/tasks/*"))
    code, output, error = cli("task", "add", "t1", "Refused contract", "--scaffold", *options)
    after = set(root.glob("*/tasks/*"))
    assert after - before == set(), (code, output, error, after - before)
    assert code != 0
    assert "task t1 was not added: " in str(code) + error
    assert '"added"' not in output
    code, output, error = cli("show")
    assert code == 0, error
    assert json.loads(output)["tasks"] == []


def test_scaffold_failure_after_acceptance_reports_the_recorded_task(cli, monkeypatch):
    def refuse(slug, task, doc=None):
        raise PermissionError("work folder refused")

    monkeypatch.setattr(ledger.ledger_workspace, "scaffold", refuse)
    code, output, error = cli("task", "add", "t1", "Recorded contract", "--scaffold")
    assert code == 0, (output, error, code)
    assert json.loads(output) == {"task": "t1", "added": "Recorded contract", "workspace_error": "work folder refused"}
    assert "task t1 was added; its work folder was not created: work folder refused" in error
    code, output, error = cli("show")
    assert code == 0, error
    task = json.loads(output)["tasks"][0]
    assert task["id"] == "t1"
    assert not Path(task["workspace"]).exists()


def test_accepted_scaffold_records_one_workspace_and_retry_keeps_it(cli):
    root = Path.home() / ".agentihooks" / "swarm"
    before = set(root.glob("*/tasks/*"))
    command = ("task", "add", "t1", "Accepted contract", "--scaffold", "--description", "Observe acceptance")
    code, output, error = cli(*command)
    assert code == 0, (output, error, code)
    code, output, error = cli("show")
    assert code == 0, error
    task = json.loads(output)["tasks"][0]
    workspace = Path(task["workspace"])
    assert set(root.glob("*/tasks/*")) - before == {workspace}
    assert {p.name for p in workspace.iterdir()} == {"steering.md", "progress.md", "proof.md"}
    assert "Observe acceptance" in (workspace / "steering.md").read_text()
    (workspace / "progress.md").write_text("Keep existing evidence")
    code, output, error = cli(*command)
    assert code != 0
    assert set(root.glob("*/tasks/*")) - before == {workspace}
    assert (workspace / "progress.md").read_text() == "Keep existing evidence"
    code, output, error = cli("show")
    assert code == 0, error
    tasks = json.loads(output)["tasks"]
    assert len(tasks) == 1
    assert tasks[0]["workspace"] == str(workspace)
