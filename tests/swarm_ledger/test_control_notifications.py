import json
import os
import subprocess
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import ledger_core as core
import ledger_server as server
import new_ledger
import pytest

from scripts.doctor import cli as doctor
from scripts.inbox.store import InboxStore
from scripts.swarm import cli
from scripts.swarm.health.findings import Finding
from tests.swarm.test_control_notifications import controls as controls
from tests.swarm_ledger import legacy_page  # noqa: E402

pytestmark = pytest.mark.unit


@pytest.fixture
def page(controls, monkeypatch):
    store, ledger, master = controls
    new_ledger.create(
        "demo",
        {
            "title": "Demo",
            "overview": "Controls",
            "phases": [{"title": "Control proof", "description": "Observe notifications"}],
        },
    )
    token = legacy_page.stored_token(core.paths("demo")[0])
    from scripts.swarm import command_runner

    monkeypatch.setattr(server, "swarm_store", lambda: store)
    monkeypatch.setattr(command_runner.shutil, "which", lambda name: "agentihooks")
    monkeypatch.setattr(cli.signal, "signal", lambda *args: None)
    monkeypatch.setattr(doctor.swarm, "connect", lambda: store)
    monkeypatch.setattr(cli, "cmd_close", lambda store, args: store.update(args.slug, state="stopped"))
    monkeypatch.setattr(cli, "cmd_reopen", lambda store, args: store.update(args.slug, state="running"))
    cli.verdict_store(store, "demo").visible(
        [Finding("idle", "worker", "Idle worker", ("Idle",), "One minute", 1)], 1, 60_000
    )
    store.create(cli.SwarmConfig("demo-doctor", "/repo", state="running", max_eng=0, max_ci=0))
    monkeypatch.setitem(doctor.COMMANDS, "start", lambda store, args: store.update("demo-doctor", state="running"))
    monkeypatch.setitem(doctor.COMMANDS, "stop", lambda store, args: store.update("demo-doctor", state="stopped"))

    def run(argv, **kwargs):
        with monkeypatch.context() as env:
            env.setattr(os, "environ", kwargs.get("env", os.environ).copy())
            code = (cli if argv[1] == "swarm" else doctor).main(argv[2:])
        return subprocess.CompletedProcess(argv, code, "", "refused" if code else "")

    monkeypatch.setattr(server.subprocess, "run", run)
    monkeypatch.setattr(server, "swarm_status", lambda slug: {"config": {"state": store.config(slug).state}})
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
    thread = threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.01})
    thread.start()

    def put(body):
        req = urllib.request.Request(
            f"http://127.0.0.1:{httpd.server_port}/api/swarm/demo",
            json.dumps(body).encode(),
            {"Host": f"127.0.0.1:{server.PORT}", "X-Ledger-Token": token},
            method="PUT",
        )
        try:
            with urllib.request.urlopen(req) as response:
                code = response.status
            command_runner.consume(store, "demo")
            return code
        except urllib.error.HTTPError as exc:
            return exc.code

    yield put, store, ledger, master
    httpd.shutdown()
    httpd.server_close()
    thread.join()


@pytest.mark.parametrize(
    ("body", "verb", "state"),
    [
        ({"action": "start"}, "started the swarm", "running"),
        ({"action": "pause"}, "paused the swarm", "paused"),
        ({"action": "stop"}, "stopped the swarm", "stopping"),
        ({"action": "stop_now"}, "stopped the swarm immediately", "stopped"),
        ({"action": "close"}, "requested closing the ledger", "stopped"),
        ({"action": "reopen"}, "reopened the ledger", "running"),
        ({"action": "set", "max_eng": 2, "max_ci": 1}, "changed the swarm settings", "running"),
        (
            {"action": "verdict", "id": "idle/worker", "verdict": "resolved", "note": "Finished"},
            "gave a health finding a verdict",
            "running",
        ),
    ],
)
def test_each_successful_page_control_notifies_once(page, body, verb, state):
    put, store, ledger, master = page
    assert put(body) == 200
    items = InboxStore(store.redis).mailbox(master.name)
    if body["action"] == "stop_now":
        assert items == []
        assert ledger.said[0][0].startswith(f"The operator {verb} from the page. The swarm is {state}.")
        return
    assert len(items) == 1
    assert items[0].fyi
    assert items[0].text.startswith(f"The operator {verb} from the page. The swarm is {state}.")
    if body["action"] == "set":
        assert "Engineer cap 2, CI cap 1, Planner cap 1." in items[0].text
    if body["action"] == "verdict":
        assert "The verdict is resolved." in items[0].text
    assert ledger.said == [(items[0].text, "swarm")]


@pytest.mark.parametrize("action", ["pause", "doctor_start"])
def test_failed_page_commands_send_nothing(page, monkeypatch, action):
    put, store, ledger, master = page

    def fail(store, args):
        raise cli.SwarmError("Control refused")

    if action == "pause":
        monkeypatch.setattr(cli, "cmd_pause", fail)
    else:
        monkeypatch.setitem(doctor.COMMANDS, "start", fail)
    from scripts.swarm import commands

    assert put({"action": action}) == 200
    assert commands.rows(store, "demo")[-1]["state"] == "failed"
    assert InboxStore(store.redis).mailbox(master.name) == []
    assert ledger.said == []


def test_invalid_page_control_sends_nothing(page):
    put, store, ledger, master = page
    assert put({"action": "set", "max_eng": -1}) == 400
    assert InboxStore(store.redis).mailbox(master.name) == []
    assert ledger.said == []


@pytest.mark.parametrize("action", ["doctor_start", "doctor_stop"])
def test_page_doctor_control_notifies_once(page, action):
    put, store, ledger, master = page
    assert put({"action": action}) == 200
    items = InboxStore(store.redis).mailbox(master.name)
    assert len(items) == 1
    assert items[0].sender == "operator"
    assert items[0].fyi
    assert "Doctor from the page" in items[0].text
    state = "running" if action == "doctor_start" else "stopped"
    assert items[0].text.endswith(f"The Doctor is {state}.")
    assert ledger.said == [(items[0].text, "swarm")]


def test_page_pause_is_attributed_to_the_operator_even_if_the_server_inherited_the_master(page, monkeypatch):
    put, store, ledger, master = page
    monkeypatch.setenv("AGENTIHOOKS_AGENT_NAME", master.name)
    assert put({"action": "pause"}) == 200
    items = InboxStore(store.redis).mailbox(master.name)
    assert len(items) == 1
    assert items[0].sender == "operator"
    assert items[0].fyi
    assert items[0].text == "The operator paused the swarm from the page. The swarm is paused."
    assert ledger.said == [(items[0].text, "swarm")]
