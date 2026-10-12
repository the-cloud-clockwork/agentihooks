import json
import os
import threading
from http.server import ThreadingHTTPServer
from types import SimpleNamespace
from unittest.mock import patch

import ledger_core as core
import new_ledger
import pytest

from scripts.hive import auth as hive_auth
from scripts.swarm import ledger_read, profile_choice, prompt
from scripts.swarm.store import SwarmError
from scripts.swarm_ledger import ledger, ledger_close, ledger_server
from scripts.swarm_ledger.api.client import ResourceClient
from scripts.swarm_ledger.repository import repository
from scripts.swarm_v2.auth_context import GrantRefused
from tests.test_swarm_v2_control_service import (
    LEDGER_TASK,
    SLUG,
    _admitted,
    _cs,
    _environ,
    _free_port,
    _get,
    _post,
    _register_and_beat,
    _store,
)

pytestmark = [pytest.mark.unit, pytest.mark.xdist_group("fakeredis")]

PINNED = {"AGENTIHOOKS_SWARM": "rig-grade-swarm", "AGENTIHOOKS_AGENT_NAME": "engineer@323133-0400"}
MASTER = "master@323133-0001"
PROGRESS = "Reading the task through the hosted ledger"
REFUSED_CONNECTION = "the hosted ledger is unavailable: <urlopen error [Errno 111] Connection refused>"


def _hl():
    from scripts.swarm_v2 import hosted_ledger

    return hosted_ledger


@pytest.fixture
def store():
    return _store()


@pytest.fixture
def hosted(store, ledger_dir):
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), ledger_server.Handler)
    host = f"127.0.0.1:{httpd.server_address[1]}"
    thread = threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
    credential = hive_auth.issue_controller(store.redis)
    remote = {"AGENTIHOOKS_DEPLOYMENT": "distributed", "LEDGER_URL": f"http://{host}", _cs().CREDENTIAL_ENV: credential}
    with (
        patch("scripts.swarm.store.redis_client", return_value=store.redis),
        patch("hooks._redis.get_redis", return_value=None),
        patch("scripts.gates.talk.Budget._marks", return_value=None),
        patch.object(ledger_server, "relay_to_inbox", return_value=None),
        patch.object(ledger_server, "doctor_phrase", return_value=None),
        patch.object(ledger_server, "ALLOWED_HOSTS", {host}),
        patch.dict(os.environ, {**remote, **PINNED}),
    ):
        thread.start()
        try:
            yield SimpleNamespace(remote=remote, folder=ledger_dir, store=store)
        finally:
            httpd.shutdown()
            httpd.server_close()
            thread.join(timeout=2)


OVERVIEW = "Tasks the hosted ledger server holds for the controller"


LEDGER_PHASE = {"title": "Ledger", "description": "Tasks read through the hosted server"}


def _hold(holder, tmp_path, overview=OVERVIEW, phases=(LEDGER_PHASE,)):
    content = {
        "title": "Hosted ledger fixture",
        "overview": overview,
        "sources": [],
        "phases": list(phases),
        "tasks": [],
    }
    document = new_ledger.build_doc(content)
    document["tasks"] = [{**LEDGER_TASK, "claimed_by": holder}]
    document, meta, _ = core.load_state(tmp_path / "seed.json", document)
    repository.create_document(SLUG, document, meta, replace=True)


def _hosted_ledger(hosted):
    return _hl().HostedLedger(hosted.remote["LEDGER_URL"], hosted.remote[_cs().CREDENTIAL_ENV], hosted.store)


def _closed():
    return f"http://127.0.0.1:{_free_port()}"


def _held_task():
    return repository.read(SLUG, "tasks/t1")["tasks"][0]


@pytest.fixture
def deployed(hosted, store, tmp_path):
    port = _free_port()
    folder = tmp_path / "controller-ledger"
    environ = {
        **_environ(tmp_path),
        **hosted.remote,
        _cs().PORT_ENV: str(port),
        _cs().SWARM_ENV: SLUG,
        "LEDGER_DIR": str(folder),
    }
    service = _cs().host(environ, store, "hive-fixture")
    try:
        agent, token = _admitted(service, store)
        _hold(agent.name, tmp_path)
        base = f"http://127.0.0.1:{port}"
        (registered, _), _ = _register_and_beat(base, service, agent, token)
        assert registered == 200
        yield SimpleNamespace(service=service, agent=agent, token=token, base=base, folder=folder)
    finally:
        service.stop()


def _read(deployed):
    return _get(f"{deployed.base}/v2/tasks/t1", deployed.token)


def _patch(deployed, revision, fields, operation_id="op-1"):
    body = {"operation_id": operation_id, "task_generation": 1, "expected_revision": revision, "fields": fields}
    return _post(f"{deployed.base}/v2/tasks/t1", deployed.token, body, "PATCH")


def test_the_deployed_controller_serves_a_task_the_hosted_server_holds(deployed):
    status, body = _read(deployed)

    assert (status, body["task_id"]) == (200, "t1")
    assert {name: body["task"][name] for name in ("id", "title", "description", "phase", "lane", "state")} == {
        name: LEDGER_TASK[name] for name in ("id", "title", "description", "phase", "lane", "state")
    }
    assert body["task"]["claimed_by"] == deployed.agent.name
    assert body["revision"] == _hl().spec_revision(body["task"])
    assert isinstance(deployed.service.tasks_api.ledger, _hl().HostedLedger)
    assert deployed.service.tasks_api.writes_enabled is True


def test_the_deployed_controller_keeps_no_ledger_folder_and_takes_no_writer_lease(deployed):
    _read(deployed)

    assert (deployed.service.writer, deployed.service.ledger_folder) == (None, None)
    assert (deployed.service.hosted_ledger.base, deployed.service.hosted_ledger.credential) == (
        os.environ["LEDGER_URL"],
        os.environ[_cs().CREDENTIAL_ENV],
    )
    assert not deployed.folder.exists()


def test_a_worker_task_update_lands_on_the_hosted_server(deployed):
    _, read = _read(deployed)

    status, ack = _patch(deployed, read["revision"], {"state": "pr"})

    held = _held_task()
    assert status == 200
    assert ack == {
        "task_id": "t1",
        "operation_id": "op-1",
        "revision": read["revision"],
        "ledger_revision": repository.get_document(SLUG)["_meta"]["rev"],
    }
    assert held["state"] == "pr"
    assert not deployed.folder.exists()


def test_a_replayed_worker_update_returns_the_same_receipt(deployed):
    _, read = _read(deployed)

    first = _patch(deployed, read["revision"], {"state": "pr"})
    again = _patch(deployed, read["revision"], {"state": "pr"})

    assert first == again
    assert first[0] == 200


def test_a_reused_operation_id_with_other_fields_is_an_operation_conflict(deployed):
    _, read = _read(deployed)
    _patch(deployed, read["revision"], {"state": "pr"})

    status, refused = _patch(deployed, read["revision"], {"state": "blocked"})

    assert (status, refused["error_class"], refused["message"]) == (
        409,
        "operation_conflict",
        "the operation ID was already used for different content",
    )
    assert _held_task()["state"] == "pr"


def test_a_stale_expected_revision_is_refused_before_the_hosted_server_is_written(deployed):
    _, read = _read(deployed)

    status, refused = _patch(deployed, "0" * 64, {"state": "pr"})

    assert (status, refused["error_class"], refused["current_revision"]) == (409, "revision_conflict", read["revision"])
    assert _held_task()["state"] == "claimed"


def test_worker_progress_lands_in_the_hosted_task_comments(deployed):
    body = {"operation_id": "op-2", "task_generation": 1, "text": PROGRESS}

    status, ack = _post(f"{deployed.base}/v2/tasks/t1/comments", deployed.token, body)

    comments = _held_task()["comments"]
    assert (status, ack["operation_id"]) == (200, "op-2")
    assert [(entry["by"], entry["text"]) for entry in comments] == [(deployed.agent.name, PROGRESS)]


def test_an_outcome_proposal_lands_on_the_hosted_server(deployed):
    _, read = _read(deployed)
    body = {
        "operation_id": "op-3",
        "task_generation": 1,
        "expected_revision": read["revision"],
        "outcome": "blocked",
        "proof": "Waiting on the hosted ledger",
    }

    status, _ = _post(f"{deployed.base}/v2/tasks/t1/outcomes", deployed.token, body)

    comments = _held_task()["comments"]
    assert status == 200
    assert [(entry["by"], entry["outcome"], entry["text"]) for entry in comments] == [
        (deployed.agent.name, "blocked", "Outcome proposal: blocked. Waiting on the hosted ledger")
    ]


class Passing:
    def __init__(self, before=None):
        self.before = before
        self.sent = None

    def apply(self, doc, op, ctx, apply_op):
        if self.before is not None:
            self.before()
        self.sent = apply_op(doc, op, ctx)
        return True


def _update(fields, holder="engineer@323133-0300"):
    return {"op": "task_update", "id": "w-direct", "by": holder, "item": "tasks/t1", "fields": fields}


def test_a_write_the_hosted_server_rejects_is_reported_rejected(hosted, tmp_path):
    _hold("engineer@323133-0300", tmp_path)
    gate = Passing()

    state, rejected = _hosted_ledger(hosted).apply_ops(SLUG, ops=[_update({"depends_on": ["t99"]})], gate=gate)

    assert (gate.sent, rejected) == (False, ["w-direct"])
    assert state["_meta"]["rev"] == repository.get_document(SLUG)["_meta"]["rev"]
    assert _held_task().get("depends_on", []) == []


def test_a_task_changed_between_read_and_write_is_a_revision_conflict(hosted, tmp_path):
    _hold("engineer@323133-0300", tmp_path)

    def race():
        changed = {"op": "task_update", "id": "race-1", "by": MASTER, "item": "tasks/t1"}
        repository.apply_ops(SLUG, ops=[{**changed, "fields": {"title": "Changed meanwhile"}}])

    with pytest.raises(_hl().RevisionConflict) as raised:
        _hosted_ledger(hosted).apply_ops(SLUG, ops=[_update({"state": "pr"})], gate=Passing(race))

    current = _hosted_ledger(hosted).read(SLUG, "tasks/t1")["tasks"][0]
    assert current["title"] == "Changed meanwhile"
    assert _held_task()["state"] == "claimed"
    assert raised.value.detail() == {
        "error_class": "revision_conflict",
        "operation_id": "unknown",
        "retry": "new_request",
        "message": "the task specification changed; refresh and retry",
        "task_id": "t1",
        "current_revision": _hl().spec_revision(current),
    }


def test_a_refused_controller_credential_on_write_is_a_dependency_failure(hosted, tmp_path):
    _hold("engineer@323133-0300", tmp_path)
    hosted_ledger = _hosted_ledger(hosted)

    def forget():
        hosted_ledger.credential = "wrong"

    with pytest.raises(GrantRefused) as raised:
        hosted_ledger.apply_ops(SLUG, ops=[_update({"state": "pr"})], gate=Passing(forget))

    assert raised.value.error_class == "dependency_unavailable"
    assert str(raised.value) == "the hosted ledger is unavailable: HTTP Error 403: Forbidden"
    assert _held_task()["state"] == "claimed"


def test_an_unreachable_hosted_server_on_write_is_a_dependency_failure(hosted, tmp_path):
    _hold("engineer@323133-0300", tmp_path)
    hosted_ledger = _hosted_ledger(hosted)

    def vanish():
        hosted_ledger.base = _closed()

    with pytest.raises(GrantRefused) as raised:
        hosted_ledger.apply_ops(SLUG, ops=[_update({"state": "pr"})], gate=Passing(vanish))

    assert raised.value.error_class == "dependency_unavailable"
    assert str(raised.value) == REFUSED_CONNECTION
    assert _held_task()["state"] == "claimed"


def test_an_unreachable_hosted_server_on_read_is_a_dependency_failure(hosted):
    with pytest.raises(GrantRefused) as raised:
        _hl().HostedLedger(_closed(), hosted.remote[_cs().CREDENTIAL_ENV], hosted.store).read(SLUG, "tasks/t1")

    assert raised.value.error_class == "dependency_unavailable"
    assert str(raised.value) == REFUSED_CONNECTION


def test_a_task_the_hosted_server_lacks_reads_as_no_task(hosted, tmp_path):
    _hold("engineer@323133-0300", tmp_path)

    assert _hosted_ledger(hosted).read(SLUG, "tasks/t9") == {"tasks": []}


def test_a_refused_controller_credential_on_read_is_a_dependency_failure(hosted, tmp_path):
    _hold("engineer@323133-0300", tmp_path)

    with pytest.raises(GrantRefused) as raised:
        _hl().HostedLedger(hosted.remote["LEDGER_URL"], "wrong", hosted.store).read(SLUG, "tasks/t1")

    assert raised.value.error_class == "dependency_unavailable"
    assert str(raised.value) == (
        "the hosted ledger is unavailable: unauthenticated: the ledger server refused the credential"
    )


def test_a_write_the_hosted_server_finds_malformed_is_an_invalid_request(hosted, tmp_path):
    _hold("engineer@323133-0300", tmp_path)
    malformed = {**_update({"state": "pr"}), "bogus": "field"}

    with pytest.raises(GrantRefused) as raised:
        _hosted_ledger(hosted).apply_ops(SLUG, ops=[malformed], gate=Passing())

    assert raised.value.error_class == "invalid_request"
    assert str(raised.value) == (
        "the ledger refused this write: Operation task_update does not match its schema at field bogus"
    )
    assert _held_task()["state"] == "claimed"


def test_a_gate_that_sends_nothing_leaves_no_revision_and_no_rejections(hosted, tmp_path):
    _hold("engineer@323133-0300", tmp_path)

    class Replayed:
        def apply(self, doc, op, ctx, apply_op):
            return True

    assert _hosted_ledger(hosted).apply_ops(SLUG, ops=[_update({"state": "pr"})], gate=Replayed()) == (
        {"_meta": {"rev": None}},
        [],
    )


def test_a_local_controller_still_mounts_its_own_ledger_folder(store, tmp_path):
    folder = tmp_path / "local-ledger"
    folder.mkdir()
    environ = {
        **_environ(tmp_path),
        _cs().PORT_ENV: str(_free_port()),
        _cs().SWARM_ENV: SLUG,
        _cs().CREDENTIAL_ENV: hive_auth.issue_controller(store.redis),
        "LEDGER_DIR": str(folder),
    }
    service = _cs().host(environ, store, "hive-fixture")
    try:
        assert (service.hosted_ledger, service.ledger_folder) == (None, folder)
    finally:
        service.stop()


def test_a_replayed_progress_after_newer_progress_is_answered_from_the_controller_receipts(deployed, store):
    url = f"{deployed.base}/v2/tasks/t1/comments"
    older = {"operation_id": "op-a", "task_generation": 1, "text": PROGRESS}
    newer = {"operation_id": "op-b", "task_generation": 1, "text": "Opened the pull request for review"}
    first = _post(url, deployed.token, older)
    _post(url, deployed.token, newer)

    with patch.object(ResourceClient, "mutate", side_effect=AssertionError("the replay reached the hosted server")):
        again = _post(url, deployed.token, older)

    assert again == first
    assert first[0] == 200
    assert [entry["text"] for entry in _held_task()["comments"]] == ["Opened the pull request for review"]
    stored = store.redis.get(store.key(SLUG, _hl().RECEIPTS_KEY, "tasks/t1"))
    assert set(json.loads(stored)["operations"]) == {"op-a", "op-b"}


def test_a_deployed_controller_without_its_credential_is_refused_at_start(hosted, store, tmp_path):
    environ = {
        **_environ(tmp_path),
        **hosted.remote,
        _cs().PORT_ENV: str(_free_port()),
        _cs().SWARM_ENV: SLUG,
        _cs().CREDENTIAL_ENV: "",
    }

    with pytest.raises(_cs().ControlError) as raised:
        _cs().host(environ, store, "hive-fixture")

    assert str(raised.value) == "the hosted ledger needs AGENTIHOOKS_CONTROLLER_CREDENTIAL"


@pytest.fixture
def controller_reads(hosted, tmp_path):
    empty = tmp_path / "controller-ledger"
    with patch.object(ledger, "BASE", ""), patch.dict(os.environ, {"LEDGER_DIR": str(empty)}):
        for name in PINNED:
            os.environ.pop(name, None)
        yield hosted
    assert not empty.exists()


def test_profile_choice_reads_project_and_phase_intent_through_the_hosted_server(controller_reads, tmp_path):
    _hold("engineer@323133-0300", tmp_path)

    state = profile_choice.state(SLUG, {"id": "t1", "phase": "p1"})

    assert (state["project_intent"], state["phase_intent"]) == (
        OVERVIEW,
        "Ledger: Tasks read through the hosted server",
    )


def test_the_hosted_read_returns_only_the_parts_asked_for(controller_reads, tmp_path):
    _hold("engineer@323133-0300", tmp_path)

    assert ledger_read.parts(SLUG, "overview") == {"overview": OVERVIEW}


def test_the_prompt_summary_reads_the_close_summary_through_the_hosted_server(controller_reads, tmp_path):
    _hold("engineer@323133-0300", tmp_path, overview=f"{OVERVIEW}{ledger_close.MARK}Stopped after the hosted reads")

    assert prompt.summary_lines(SLUG) == [
        "Ledger summary from the previous close:",
        "Summary\nStopped after the hosted reads",
        "In your first ledger chat line, name this summary and say where the work stopped in plain words.",
    ]


def test_a_ledger_the_hosted_server_lacks_reads_as_empty(controller_reads):
    assert ledger_read.parts("absent-ledger", "overview", "phases") == {}


def test_an_unreachable_hosted_server_fails_the_intent_read_as_a_swarm_error(controller_reads):
    os.environ["LEDGER_URL"] = _closed()

    with pytest.raises(SwarmError) as raised:
        ledger_read.parts(SLUG, "overview", "phases")

    assert str(raised.value) == (
        f"the hosted ledger is unavailable for {SLUG}: <urlopen error [Errno 111] Connection refused>"
    )


def test_an_operation_id_the_hosted_server_already_holds_for_other_content_is_an_operation_conflict(hosted, tmp_path):
    _hold("engineer@323133-0300", tmp_path)
    hosted_ledger = _hosted_ledger(hosted)
    hosted_ledger.apply_ops(SLUG, ops=[_update({"state": "pr"})], gate=Passing())

    with pytest.raises(GrantRefused) as raised:
        hosted_ledger.apply_ops(SLUG, ops=[_update({"state": "blocked"})], gate=Passing())

    assert (raised.value.error_class, str(raised.value)) == (
        "operation_conflict",
        "the operation ID was already used for different content",
    )
    assert _held_task()["state"] == "pr"


def test_the_hosted_read_returns_every_phase_past_the_first_page(controller_reads, tmp_path):
    phases = [{"title": f"Phase {n}", "description": "One of many phases"} for n in range(1, 53)]
    _hold("engineer@323133-0300", tmp_path, phases=phases)

    read = ledger_read.parts(SLUG, "phases")["phases"]

    assert [phase["title"] for phase in read] == [phase["title"] for phase in phases]


def test_a_local_host_reads_intent_from_its_ledger_folder(hosted, tmp_path):
    _hold("engineer@323133-0300", tmp_path)
    with patch.dict(os.environ):
        os.environ.pop("AGENTIHOOKS_DEPLOYMENT")
        read = ledger_read.parts(SLUG, "overview", "phases")
        absent = ledger_read.parts("absent-ledger", "overview")

    assert (read["overview"], [phase["title"] for phase in read["phases"]], absent) == (OVERVIEW, ["Ledger"], {})
